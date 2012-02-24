import os
import zlib
import sqlite3
import logging
import json
from gettext import gettext as _
from pkg_resources import parse_version
import urllib
from urlparse import urlparse


has_mapnik = False
try:
    import mapnik
    has_mapnik = True
except ImportError:
    pass


from . import DEFAULT_TILE_SIZE, DOWNLOAD_RETRIES
from proj import GoogleProjection


logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    """ Raised when extraction of tiles from specified MBTiles has failed """
    pass

class InvalidFormatError(Exception):
    """ Raised when reading of MBTiles content has failed """
    pass

class DownloadError(Exception):
    """ Raised when download at tiles URL fails DOWNLOAD_RETRIES times """
    pass


class Reader(object):
    def __init__(self, tilesize=None):
        if not tilesize:
            tilesize = DEFAULT_TILE_SIZE
        self.tilesize = tilesize
        self.basename = ''

    def tile(self, z, x, y, output):
        raise NotImplementedError


class MBTilesReader(Reader):
    def __init__(self, filename, tilesize=None):
        super(MBTilesReader, self).__init__(tilesize)
        self.filename = filename
        self.basename = os.path.basename(self.filename)
        self._con = None
        self._cur = None

    def _query(self, sql, *args):
        """ Executes the specified `sql` query and returns the cursor """
        if not self._con:
            logger.debug(_("Open MBTiles file '%s'") % self.filename)
            self._con = sqlite3.connect(self.filename)
            self._cur = self._con.cursor()
        sql = ' '.join(sql.split())
        logger.debug(_("Execute query '%s' %s") % (sql, args))
        try:
            self._cur.execute(sql, *args)
        except (sqlite3.OperationalError, sqlite3.DatabaseError), e:
            raise InvalidFormatError(_("%s while reading %s") % (e, self.filename))
        return self._cur

    def metadata(self):
        rows = self._query('SELECT name, value FROM metadata')
        rows = [(row[0], row[1]) for row in rows]
        return dict(rows)

    def zoomlevels(self):
        rows = self._query('SELECT DISTINCT(zoom_level) FROM tiles ORDER BY zoom_level')
        return [int(row[0]) for row in rows]

    def tile(self, z, x, y, output):
        logger.debug(_("Extract tile %s") % (z, x, y))
        y_mercator = (2**int(z) - 1) - int(y)
        rows = self._query('''SELECT tile_data FROM tiles 
                              WHERE zoom_level=? AND tile_column=? AND tile_row=?;''', (z, x, y_mercator))
        t = rows.fetchone()
        if not t:
            raise ExtractionError(_("Could not extract tile %s from %s") % ((z, x, y), self.filename))
        with open(output, 'wb') as out:
            out.write(t[0])

    def grid(self, z, x, y, callback=None):
        if not callback:
            callback = 'grid'

        y_mercator = (2**int(z) - 1) - int(y)
        rows = self._query('''SELECT grid FROM grids 
                              WHERE zoom_level=? AND tile_column=? AND tile_row=?;''', (z, x, y_mercator))
        t = rows.fetchone()
        if not t:
            raise ExtractionError(_("Could not extract grid %s from %s") % ((z, x, y), self.filename))
        grid_json = json.loads(zlib.decompress(t[0]))
        
        rows = self._query('''SELECT key_name, key_json FROM grid_data
                              WHERE zoom_level=? AND tile_column=? AND tile_row=?;''', (z, x, y_mercator))
        # join up with the grid 'data' which is in pieces when stored in mbtiles file
        grid_json['data'] = {}
        grid_data = rows.fetchone()
        while grid_data:
            grid_json['data'][grid_data[0]] = json.loads(grid_data[1])
            grid_data = rows.fetchone()
        return '%s(%s);' % (callback, json.dumps(grid_json))

    def find_coverage(self, zoom):
        """
        Returns the bounding box (minx, miny, maxx, maxy) of an adjacent 
        group of tiles at this zoom level.
        """
        # Find a group of adjacent available tiles at this zoom level
        rows = self._query('''SELECT tile_column, tile_row FROM tiles 
                              WHERE zoom_level=? 
                              ORDER BY tile_column, tile_row;''', (zoom,))
        t = rows.fetchone()
        xmin, ymin = t
        previous = t
        while t and t[0] - previous[0] <= 1:
            # adjacent, go on
            previous = t
            t = rows.fetchone()
        xmax, ymax = previous
        # Transform (xmin, ymin) (xmax, ymax) to pixels
        S = self.tilesize
        bottomleft = (xmin * S, (ymax + 1) * S)
        topright = ((xmax + 1) * S, ymin * S)
        # Convert center to (lon, lat)
        proj = GoogleProjection(S, [zoom])  # WGS84
        return proj.unproject_pixels(bottomleft, zoom) + proj.unproject_pixels(topright, zoom)


class TileDownloader(Reader):
    def __init__(self, url, subdomains, tilesize=None):
        super(TileDownloader, self).__init__(tilesize)
        self.tiles_url = url
        self.tiles_subdomains = subdomains
        parsed = urlparse(self.tiles_url)
        self.basename = parsed.netloc

    def tile(self, z, x, y, output):
        """
        Download the specified tile from `tiles_url`
        """
        logger.debug(_("Download tile %s") % ((z, x, y),))
        # Render each keyword in URL ({s}, {x}, {y}, {z}, {size} ... )
        size = self.tilesize
        s = self.tiles_subdomains[(x + y) % len(self.tiles_subdomains)];
        try:
            url = self.tiles_url.format(**locals())
        except KeyError, e:
            raise DownloadError(_("Unknown keyword %s in URL") % e)
        
        logger.debug(_("Retrieve tile at %s") % url)
        r = DOWNLOAD_RETRIES
        while r > 0:
            try:
                image = urllib.URLopener()
                image.retrieve(url, output)
                return  # Done.
            except IOError, e:
                logger.debug(_("Download error, retry (%s left). (%s)") % (r, e))
                r -= 1
        raise DownloadError


class WMSReader(Reader):
    def __init__(self, url, layers, tilesize=None, **kwargs):
        super(WMSReader, self).__init__(tilesize)
        self.basename = '-'.join(layers)
        self.url = url
        self.wmsParams = dict(
            service='WMS',
            request='GetMap',
            version='1.1.1',
            styles='',
            format='image/jpeg',
            transparent=False,
            layers=','.join(layers),
            width=self.tilesize,
            height=self.tilesize,
        )
        self.wmsParams.update(**kwargs)
        projectionKey = 'srs'
        if parse_version(self.wmsParams['version']) >= parse_version('1.3'):
            projectionKey = 'crs'
        self.wmsParams[projectionKey] = GoogleProjection.NAME

    def tile(self, z, x, y, output):
        logger.debug(_("Request WMS tile %s") % ((z, x, y),))
        proj = GoogleProjection(self.tilesize, [z])
        bbox = proj.tile_bbox((z, x, y))
        bbox = proj.project(bbox[:2]) + proj.project(bbox[2:])
        bbox = ','.join(map(str, bbox))
        # Build WMS request URL 
        encodedparams = urllib.urlencode(self.wmsParams)
        url = "%s?%s" % (self.url, encodedparams)
        url += "&bbox=%s" % bbox   # commas are not encoded
        try:
            logger.debug(_("Download '%s'") % url)
            f = urllib.urlopen(url)
            header = f.info().typeheader
            assert header == self.wmsParams['format'], "Invalid WMS response type : %s" % header
            with open(output, 'wb') as out:
                out.write(f.read())
        except (AssertionError, IOError), e:
            raise ExtractionError


class MapnikRenderer(Reader):
    def __init__(self, stylefile, tilesize=None):
        super(MapnikRenderer, self).__init__(tilesize)
        self.stylefile = stylefile
        self.basename = os.path.basename(self.stylefile)
        self._mapnik = None

    def tile(self, z, x, y, output):
        """
        Render the specified tile with Mapnik
        """
        logger.debug(_("Render tile %s") % (z, x, y))
        return self.render(proj.tile_bbox((z, x, y)), output)

    def render(self, bbox, output):
        """
        Render the specified tile with Mapnik
        """
        if not self._mapnik:
            self._mapnik = mapnik.Map(width, height)
            # Load style XML
            mapnik.load_map(self._mapnik, self.stylefile, True)
            # Obtain <Map> projection
            self._prj = mapnik.Projection(self._mapnik.srs)

        # Convert to map projection
        assert len(bbox) == 4, _("Provide a bounding box tuple (minx, miny, maxx, maxy)")
        c0 = self._prj.forward(mapnik.Coord(bbox[0], bbox[1]))
        c1 = self._prj.forward(mapnik.Coord(bbox[2], bbox[3]))

        # Bounding box for the tile
        if hasattr(mapnik,'mapnik_version') and mapnik.mapnik_version() >= 800:
            bbox = mapnik.Box2d(c0.x, c0.y, c1.x, c1.y)
        else:
            bbox = mapnik.Envelope(c0.x, c0.y, c1.x, c1.y)
        
        self._mapnik.resize(width, height)
        self._mapnik.zoom_to_box(bbox)
        self._mapnik.buffer_size = 128

        # Render image with default Agg renderer
        im = mapnik.Image(width, height)
        mapnik.render(self._mapnik, im)
        im.save(output, 'png256')
