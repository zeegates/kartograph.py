
from options import parse_options
from layersource import handle_layer_source
from geometry import BBox, View, MultiPolygon
from geometry.utils import geom_to_bbox
from shapely.geometry.base import BaseGeometry
from shapely.geometry import Polygon, LineString, MultiPolygon, MultiLineString
from proj import projections
from filter import filter_record
from errors import *


class Kartograph(object):
    """
    main class of Kartograph
    """
    def __init__(self):
        self.layerCache = {}
        pass

    def generate(self, opts, outfile=None, preview=True):
        """
        generates svg map
        """
        parse_options(opts)
        self._verbose = 'verbose' in opts and opts['verbose']
        self.prepare_layers(opts)

        layers = []
        layerOpts = {}

        for layer in opts['layers']:
            id = layer['id']
            layerOpts[id] = layer
            layers.append(id)

        proj = self.get_projection(opts, layerOpts)
        bounds_poly = self.get_bounds(opts, proj, layerOpts)

        #_plot_geometry(bounds_poly)

        bbox = geom_to_bbox(bounds_poly)

        view = self.get_view(opts, bbox)
        w = view.width
        h = view.height
        view_poly = Polygon([(0, 0), (0, h), (w, h), (w, 0)])

        svg = self.init_svg_canvas(opts, proj, view, bbox)

        layerFeatures = {}

        # get features
        for layer in opts['layers']:
            id = layer['id']
            features = self.get_features(layer, proj, view, opts, view_poly)
            layerFeatures[id] = features

        #_debug_show_features(layerFeatures[id], 'original')
        self.join_layers(layers, layerOpts, layerFeatures)
        #_debug_show_features(layerFeatures[id], 'joined')
        self.crop_layers_to_view(layers, layerFeatures, view_poly)
        #_debug_show_features(layerFeatures[id], 'cropped to view')
        self.simplify_layers(layers, layerFeatures, layerOpts)
        #_debug_show_features(layerFeatures[id], 'simplified')
        #exit()

        #self.crop_layers(layers, layerOpts, layerFeatures)
        #self.substract_layers(layers, layerOpts, layerFeatures)
        self.store_layers_svg(layers, layerOpts, layerFeatures, svg, opts)

        if outfile is None:
            if preview:
                svg.preview()
            else:
                return svg.tostring()
        else:
            svg.save(outfile)

    def prepare_layers(self, opts):
        """
        prepares layer sources
        """
        self.layers = layers = {}

        for layer in opts['layers']:
            id = layer['id']
            while id in layers:
                id += "_"
            if id != layer['id']:
                layer['id'] = id  # rename layer
            src = handle_layer_source(layer, self.layerCache)
            layers[id] = src

    def get_projection(self, opts, layerOpts):
        """
        instantiates the map projection
        """
        map_center = self.get_map_center(opts, layerOpts)
        projC = projections[opts['proj']['id']]
        p_opts = {}
        for prop in opts['proj']:
            if prop != "id":
                p_opts[prop] = opts['proj'][prop]
            if prop == "lon0" and p_opts[prop] == "auto":
                p_opts[prop] = map_center[0]
            elif prop == "lat0" and p_opts[prop] == "auto":
                p_opts[prop] = map_center[1]
        proj = projC(**p_opts)
        return proj

    def get_map_center(self, opts, layerOpts):
        """
        depends on the bounds config
        """
        mode = opts['bounds']['mode']
        data = opts['bounds']['data']

        lon0 = 0

        if mode == 'bbox':
            lon0 = data[0] + 0.5 * (data[2] - data[0])
            lat0 = data[1] + 0.5 * (data[3] - data[1])

        elif mode[:5] == 'point':
            lon0 = 0
            lat0 = 0
            m = 1 / len(data)
            for (lon, lat) in data:
                lon0 += m * lon
                lat0 += m * lat

        elif mode[:4] == 'poly':
            features = self.get_bounds_polygons(opts, layerOpts)
            if len(features) > 0:
                if isinstance(features[0].geom, BaseGeometry):
                    (lon0, lat0) = features[0].geom.representative_point().coords[0]
            else:
                lon0 = 0
                lat0 = 0
        else:
            print "unrecognized bound mode", mode
        return (lon0, lat0)

    def get_bounds(self, opts, proj, layerOpts):
        """
        computes the (x,y) bounding box for the map,
        given a specific projection
        """
        from geometry.utils import bbox_to_polygon, geom_to_bbox

        bnds = opts['bounds']
        mode = bnds['mode'][:]
        data = bnds['data']

        if self._verbose:
            print 'bounds mode', mode

        if mode == "bbox":  # catch special case bbox
            sea = proj.bounding_geometry(data, projected=True)
            sbbox = geom_to_bbox(sea)
            sbbox.inflate(sbbox.width * bnds['padding'])
            return bbox_to_polygon(sbbox)

        bbox = BBox()

        if mode[:5] == "point":
            for lon, lat in data:
                pt = proj.project(lon, lat)
                bbox.update(pt)

        if mode[:4] == "poly":
            features = self.get_bounds_polygons(opts, layerOpts)
            if len(features) > 0:
                for feature in features:
                    feature.project(proj)
                    fbbox = geom_to_bbox(feature.geometry, data["min-area"])
                    bbox.join(fbbox)
            else:
                raise KartographError('no features found for calculating the map bounds')
        bbox.inflate(bbox.width * bnds['padding'])
        return bbox_to_polygon(bbox)

    def get_bounds_polygons(self, opts, layerOpts):
        """
        for bounds mode "polygons" this helper function
        returns a list of all polygons that the map should
        be cropped to
        """
        features = []
        data = opts['bounds']['data']
        id = data['layer']
        if id not in self.layers:
            raise KartographError('layer not found "%s"' % id)
        layer = self.layers[id]
        layerOpts = layerOpts[id]

        if layerOpts['filter'] is False:
            layerFilter = lambda a: True
        else:
            layerFilter = lambda rec: filter_record(layerOpts['filter'], rec)

        if data['filter']:
            boundsFilter = lambda rec: filter_record(data['filter'], rec)
        else:
            boundsFilter = lambda a: True

        filter = lambda rec: layerFilter(rec) and boundsFilter(rec)
        features = layer.get_features(filter=filter)
        return features

    def get_view(self, opts, bbox):
        """
        returns the output view
        """
        exp = opts["export"]
        w = exp["width"]
        h = exp["height"]
        ratio = exp["ratio"]

        if ratio == "auto":
            ratio = bbox.width / float(bbox.height)

        if h == "auto":
            h = w / ratio
        elif w == "auto":
            w = h * ratio
        return View(bbox, w, h - 1)

    def get_features(self, layer, proj, view, opts, view_poly):
        """
        returns a list of projected and filtered features of a layer
        """
        id = layer['id']
        src = self.layers[id]
        is_projected = False

        bbox = [-180, -90, 180, 90]
        if opts['bounds']['mode'] == "bbox":
            bbox = opts['bounds']['data']
        if 'crop' in opts['bounds']:
            bbox = opts['bounds']['crop']

        if 'src' in layer:  # regular geodata layer
            if layer['filter'] is False:
                filter = None
            else:
                filter = lambda rec: filter_record(layer['filter'], rec)
            features = src.get_features(filter=filter, bbox=bbox, verbose=self._verbose)

        elif 'special' in layer:  # special layers need special treatment
            if layer['special'] == "graticule":
                lats = layer['latitudes']
                lons = layer['longitudes']
                features = src.get_features(lats, lons, proj, bbox=bbox)

            elif layer['special'] == "sea":
                features = src.get_features(proj.sea_shape())
                is_projected = True

        for feature in features:
            if not is_projected:
                feature.project(proj)
            feature.project_view(view)

        # remove features that don't intersect our view polygon
        features = [feature for feature in features if feature.geometry and feature.geometry.intersects(view_poly)]

        return features

    def init_svg_canvas(self, opts, proj, view, bbox):
        """
        prepare a blank new svg file
        """
        import svg as svgdoc

        w = view.width
        h = view.height + 2

        svg = svgdoc.Document(width='%dpx' % w, height='%dpx' % h, viewBox='0 0 %d %d' % (w, h), enable_background='new 0 0 %d %d' % (w, h), style='stroke-linejoin: round; stroke:#000; fill:#f6f3f0;')
        defs = svg.node('defs', svg.root)
        style = svg.node('style', defs, type='text/css')
        css = 'path { fill-rule: evenodd; }\n#context path { fill: #eee; stroke: #bbb; } '
        svg.cdata(css, style)
        metadata = svg.node('metadata', svg.root)
        views = svg.node('views', metadata)
        view = svg.node('view', views, padding=str(opts['bounds']['padding']), w=w, h=h)

        svg.node('proj', view, **proj.attrs())
        bbox = svg.node('bbox', view, x=round(bbox.left, 2), y=round(bbox.top, 2), w=round(bbox.width, 2), h=round(bbox.height, 2))

        ll = [-180, -90, 180, 90]
        if opts['bounds']['mode'] == "bbox":
            ll = opts['bounds']['data']
        svg.node('llbbox', view, lon0=ll[0], lon1=ll[2], lat0=ll[1], lat1=ll[3])

        return svg

    def simplify_layers(self, layers, layerFeatures, layerOpts):
        """
        performs polygon simplification
        """
        from simplify import create_point_store, simplify_lines

        point_store = create_point_store()  # create a new empty point store

        # compute topology for all layers
        for id in layers:
            if layerOpts[id]['simplify'] is not False:
                for feature in layerFeatures[id]:
                    feature.compute_topology(point_store, layerOpts[id]['unify-precision'])

        # break features into lines
        for id in layers:
            if layerOpts[id]['simplify'] is not False:
                for feature in layerFeatures[id]:
                    feature.break_into_lines()

        # simplify lines
        total = 0
        kept = 0
        for id in layers:
            if layerOpts[id]['simplify'] is not False:
                for feature in layerFeatures[id]:
                    lines = feature.break_into_lines()
                    lines = simplify_lines(lines, layerOpts[id]['simplify']['method'], layerOpts[id]['simplify']['tolerance'])
                    for line in lines:
                        total += len(line)
                        for pt in line:
                            if not pt.deleted:
                                kept += 1
                    feature.restore_geometry(lines, layerOpts[id]['filter-islands'])
        return (total, kept)

    def crop_layers_to_view(self, layers, layerFeatures, view_poly):
        """
        cuts the layer features to the map view
        """
        for id in layers:
            #out = []
            for feat in layerFeatures[id]:
                if not feat.geometry.is_valid:
                    pass
                    #print feat.geometry
                    #_plot_geometry(feat.geometry)
                feat.crop_to(view_poly)
                #if not feat.is_empty():
                #    out.append(feat)
            #layerFeatures[id] = out

    def crop_layers(self, layers, layerOpts, layerFeatures):
        """
        handles crop-to
        """
        for id in layers:
            if layerOpts[id]['crop-to'] is not False:
                cropped_features = []
                for tocrop in layerFeatures[id]:
                    cbbox = tocrop.geom.bbox()
                    crop_at_layer = layerOpts[id]['crop-to']
                    if crop_at_layer not in layers:
                        raise KartographError('you want to substract from layer "%s" which cannot be found' % crop_at_layer)
                    for crop_at in layerFeatures[crop_at_layer]:
                        if crop_at.geom.bbox().intersects(cbbox):
                            tocrop.crop_to(crop_at.geom)
                            cropped_features.append(tocrop)
                layerFeatures[id] = cropped_features

    def substract_layers(self, layers, layerOpts, layerFeatures):
        """
        handles substract-from
        """
        for id in layers:
            if layerOpts[id]['subtract-from'] is not False:
                for feat in layerFeatures[id]:
                    cbbox = feat.geom.bbox()
                    for subid in layerOpts[id]['subtract-from']:
                        if subid not in layers:
                            raise KartographError('you want to substract from layer "%s" which cannot be found' % subid)
                        for sfeat in layerFeatures[subid]:
                            if sfeat.geom.bbox().intersects(cbbox):
                                sfeat.substract_geom(feat.geom)
                layerFeatures[id] = []

    def join_layers(self, layers, layerOpts, layerFeatures):
        """
        joins features in layers
        """
        from geometry.utils import join_features

        for id in layers:
            if layerOpts[id]['join'] is not False:
                unjoined = 0
                join = layerOpts[id]['join']
                groupBy = join['group-by']
                groups = join['groups']
                if not groups:
                    # auto populate groups
                    groups = {}
                    for feat in layerFeatures[id]:
                        fid = feat.props[groupBy]
                        groups[fid] = [fid]

                groupAs = join['group-as']
                groupFeatures = {}
                res = []
                for feat in layerFeatures[id]:
                    found_in_group = False
                    for g_id in groups:
                        if g_id not in groupFeatures:
                            groupFeatures[g_id] = []
                        if feat.props[groupBy] in groups[g_id] or str(feat.props[groupBy]) in groups[g_id]:
                            groupFeatures[g_id].append(feat)
                            found_in_group = True
                            break
                    if not found_in_group:
                        unjoined += 1
                        res.append(feat)
                #print unjoined,'features were not joined'
                for g_id in groups:
                    props = {}
                    for feat in groupFeatures[g_id]:
                        fprops = feat.props
                        for key in fprops:
                            if key not in props:
                                props[key] = fprops[key]
                            else:
                                if props[key] != fprops[key]:
                                    props[key] = "---"

                    if groupAs is not False:
                        props[groupAs] = g_id
                    if g_id in groupFeatures:
                        res += join_features(groupFeatures[g_id], props)
                layerFeatures[id] = res

    def store_layers_svg(self, layers, layerOpts, layerFeatures, svg, opts):
        """
        store features in svg
        """
        for id in layers:
            if self._verbose:
                print id
            if len(layerFeatures[id]) == 0:
                print "ignoring layer", id
                continue  # ignore empty layers
            g = svg.node('g', svg.root, id=id)
            for feat in layerFeatures[id]:
                node = feat.to_svg(svg, opts['export']['round'], layerOpts[id]['attributes'])
                if node is not None:
                    g.appendChild(node)
                else:
                    print "feature.to_svg is None", feat
            if 'styles' in layerOpts[id]:
                for prop in layerOpts[id]['styles']:
                    g.setAttribute(prop, str(layerOpts[id]['styles'][prop]))

    def generate_kml(self, opts, outfile=None):
        """
        generates KML file
        """
        parse_options(opts)
        self.prepare_layers(opts)

        #proj = self.get_projection(opts)
        #bounds_poly = self.get_bounds(opts,proj)
        #bbox = bounds_poly.bbox()

        proj = projections['ll']()
        view = View()

        #view = self.get_view(opts, bbox)
        #w = view.width
        #h = view.height
        #view_poly = MultiPolygon([[(0,0),(0,h),(w,h),(w,0)]])
        # view_poly = bounds_poly.project_view(view)
        view_poly = None

        kml = self.init_kml_canvas()

        layers = []
        layerOpts = {}
        layerFeatures = {}

        # get features
        for layer in opts['layers']:
            id = layer['id']
            layerOpts[id] = layer
            layers.append(id)
            features = self.get_features(layer, proj, view, opts, view_poly)
            layerFeatures[id] = features

        self.simplify_layers(layers, layerFeatures, layerOpts)
        # self.crop_layers_to_view(layers, layerFeatures, view_poly)
        self.crop_layers(layers, layerOpts, layerFeatures)
        self.join_layers(layers, layerOpts, layerFeatures)
        self.substract_layers(layers, layerOpts, layerFeatures)
        self.store_layers_kml(layers, layerOpts, layerFeatures, kml, opts)

        if outfile is None:
            outfile = open('tmp.kml', 'w')

        from lxml import etree
        outfile.write(etree.tostring(kml, pretty_print=True))

    def init_kml_canvas(self):
        from pykml.factory import KML_ElementMaker as KML
        kml = KML.kml(
            KML.Document(
                KML.name('kartograph map')
            )
        )
        return kml

    def store_layers_kml(self, layers, layerOpts, layerFeatures, kml, opts):
        """
        store features in kml (projected to WGS84 latlon)
        """
        from pykml.factory import KML_ElementMaker as KML

        for id in layers:
            if self._verbose:
                print id
            if len(layerFeatures[id]) == 0:
                continue  # ignore empty layers
            g = KML.Folder(
                KML.name(id)
            )
            for feat in layerFeatures[id]:
                g.append(feat.to_kml(opts['export']['round'], layerOpts[id]['attributes']))
            kml.Document.append(g)


def _plot_geometry(geom, fill='#ffcccc', stroke='#333333', alpha=1, msg=None):
    from matplotlib import pyplot
    from matplotlib.figure import SubplotParams
    from descartes import PolygonPatch

    if isinstance(geom, (Polygon, MultiPolygon)):
        b = geom.bounds
        # b = (min(c[0], b[0]), min(c[1], b[1]), max(c[2], b[2]), max(c[3], b[3]))
        geoms = hasattr(geom, 'geoms') and geom.geoms or [geom]
        w, h = (b[2] - b[0], b[3] - b[1])
        ratio = w / h
        pad = 0.15
        fig = pyplot.figure(1, figsize=(5, 5 / ratio), dpi=110, subplotpars=SubplotParams(left=pad, bottom=pad, top=1 - pad, right=1 - pad))
        ax = fig.add_subplot(111, aspect='equal')
        for geom in geoms:
            patch1 = PolygonPatch(geom, linewidth=0.5, fc=fill, ec=stroke, alpha=alpha, zorder=0)
            ax.add_patch(patch1)
    p = (b[2] - b[0]) * 0.03  # some padding
    pyplot.axis([b[0] - p, b[2] + p, b[3] + p, b[1] - p])
    #ax.xaxis.set_visible(False)
    #ax.yaxis.set_visible(False)
    #ax.set_frame_on(False)
    pyplot.grid(True)
    if msg:
        fig.suptitle(msg, y=0.04, fontsize=9)
    pyplot.show()


def _plot_lines(lines):
    from matplotlib import pyplot

    def plot_line(ax, line):
        filtered = []
        for pt in line:
            if not pt.deleted:
                filtered.append(pt)
        if len(filtered) < 2:
            return
        ob = LineString(line)
        x, y = ob.xy
        ax.plot(x, y, '-', color='#333333', linewidth=0.5, solid_capstyle='round', zorder=1)

        #ob = LineString(filtered)
        #x, y = ob.xy
        #ax.plot(x, y, '-', color='#dd4444', linewidth=1, alpha=0.5, solid_capstyle='round', zorder=1)
        #ax.plot(x[0], y[0], 'o', color='#cc0000', zorder=3)
        #ax.plot(x[-1], y[-1], 'o', color='#cc0000', zorder=3)

    fig = pyplot.figure(1, figsize=(4, 5.5), dpi=90, subplotpars=SubplotParams(left=0, bottom=0.065, top=1, right=1))
    ax = fig.add_subplot(111, aspect='equal')
    for line in lines:
        plot_line(ax, line)
    pyplot.grid(False)
    ax.xaxis.set_visible(False)
    ax.yaxis.set_visible(False)
    ax.set_frame_on(False)
    return (ax, fig)


def _debug_show_features(features, message=None):
    """ for debugging purposes we're going to output the features """
    from descartes import PolygonPatch
    from matplotlib import pyplot
    from matplotlib.figure import SubplotParams

    fig = pyplot.figure(1, figsize=(9, 5.5), dpi=110, subplotpars=SubplotParams(left=0, bottom=0.065, top=1, right=1))
    ax = fig.add_subplot(111, aspect='equal')
    b = (100000, 100000, -100000, -100000)
    for feat in features:
        if feat.geom is None:
            continue
        c = feat.geom.bounds
        b = (min(c[0], b[0]), min(c[1], b[1]), max(c[2], b[2]), max(c[3], b[3]))
        geoms = hasattr(feat.geom, 'geoms') and feat.geom.geoms or [feat.geom]
        for geom in geoms:
            patch1 = PolygonPatch(geom, linewidth=0.25, fc='#ddcccc', ec='#000000', alpha=0.75, zorder=0)
            ax.add_patch(patch1)
    p = (b[2] - b[0]) * 0.05  # some padding
    pyplot.axis([b[0] - p, b[2] + p, b[3], b[1] - p])
    ax.xaxis.set_visible(False)
    ax.yaxis.set_visible(False)
    ax.set_frame_on(True)
    if message:
        fig.suptitle(message, y=0.04, fontsize=9)
    pyplot.show()
