"""CherryPy dispatchers."""

import cherrypy


class PageHandler(object):
    """Callable which sets response.body."""
    
    def __init__(self, callable, *args, **kwargs):
        self.callable = callable
        self.args = args
        self.kwargs = kwargs
    
    def __call__(self):
        return self.callable(*self.args, **self.kwargs)


class LateParamPageHandler(PageHandler):
    """When passing cherrypy.request.params to the page handler, we do not
    want to capture that dict too early; we want to give tools like the
    decoding tool a chance to modify the params dict in-between the lookup
    of the handler and the actual calling of the handler. This subclass
    takes that into account, and allows request.params to be 'bound late'
    (it's more complicated than that, but that's the effect).
    """
    
    def _get_kwargs(self):
        kwargs = cherrypy.request.params.copy()
        if self._kwargs:
            kwargs.update(self._kwargs)
        return kwargs
    
    def _set_kwargs(self, kwargs):
        self._kwargs = kwargs
    
    kwargs = property(_get_kwargs, _set_kwargs,
                      doc='page handler kwargs (with '
                      'cherrypy.request.params copied in)')


class Dispatcher(object):
    
    def __call__(self, path_info):
        """Set handler and config for the current request."""
        request = cherrypy.request
        func, vpath = self.find_handler(path_info)
        
        if func:
            # Decode any leftover %2F in the virtual_path atoms.
            vpath = [x.replace("%2F", "/") for x in vpath]
            request.handler = LateParamPageHandler(func, *vpath)
        else:
            request.handler = cherrypy.NotFound()
    
    def find_handler(self, path):
        """Return the appropriate page handler, plus any virtual path.
        
        This will return two objects. The first will be a callable,
        which can be used to generate page output. Any parameters from
        the query string or request body will be sent to that callable
        as keyword arguments.
        
        The callable is found by traversing the application's tree,
        starting from cherrypy.request.app.root, and matching path
        components to successive objects in the tree. For example, the
        URL "/path/to/handler" might return root.path.to.handler.
        
        The second object returned will be a list of names which are
        'virtual path' components: parts of the URL which are dynamic,
        and were not used when looking up the handler.
        These virtual path components are passed to the handler as
        positional arguments.
        """
        request = cherrypy.request
        app = request.app
        root = app.root
        
        # Get config for the root object/path.
        curpath = ""
        nodeconf = {}
        if hasattr(root, "_cp_config"):
            nodeconf.update(root._cp_config)
        if "/" in app.config:
            nodeconf.update(app.config["/"])
        object_trail = [['root', root, nodeconf, curpath]]
        
        node = root
        names = [x for x in path.strip('/').split('/') if x] + ['index']
        for name in names:
            # map to legal Python identifiers (replace '.' with '_')
            objname = name.replace('.', '_')
            
            nodeconf = {}
            node = getattr(node, objname, None)
            if node is not None:
                # Get _cp_config attached to this node.
                if hasattr(node, "_cp_config"):
                    nodeconf.update(node._cp_config)
            
            # Mix in values from app.config for this path.
            curpath = "/".join((curpath, name))
            if curpath in app.config:
                nodeconf.update(app.config[curpath])
            
            object_trail.append([name, node, nodeconf, curpath])
        
        def set_conf():
            """Set cherrypy.request.config."""
            base = cherrypy.config.copy()
            # Note that we merge the config from each node
            # even if that node was None.
            for name, obj, conf, curpath in object_trail:
                base.update(conf)
                if 'tools.staticdir.dir' in conf:
                    base['tools.staticdir.section'] = curpath
            return base
        
        # Try successive objects (reverse order)
        num_candidates = len(object_trail) - 1
        for i in xrange(num_candidates, -1, -1):
            
            name, candidate, nodeconf, curpath = object_trail[i]
            if candidate is None:
                continue
            
            # Try a "default" method on the current leaf.
            if hasattr(candidate, "default"):
                defhandler = candidate.default
                if getattr(defhandler, 'exposed', False):
                    # Insert any extra _cp_config from the default handler.
                    conf = getattr(defhandler, "_cp_config", {})
                    object_trail.insert(i+1, ["default", defhandler, conf, curpath])
                    request.config = set_conf()
                    request.is_index = False
                    return defhandler, names[i:-1]
            
            # Uncomment the next line to restrict positional params to "default".
            # if i < num_candidates - 2: continue
            
            # Try the current leaf.
            if getattr(candidate, 'exposed', False):
                request.config = set_conf()
                if i == num_candidates:
                    # We found the extra ".index". Mark request so tools
                    # can redirect if path_info has no trailing slash.
                    request.is_index = True
                else:
                    # We're not at an 'index' handler. Mark request so tools
                    # can redirect if path_info has NO trailing slash.
                    # Note that this also includes handlers which take
                    # positional parameters (virtual paths).
                    request.is_index = False
                return candidate, names[i:-1]
        
        # We didn't find anything
        request.config = set_conf()
        return None, []


class MethodDispatcher(Dispatcher):
    """Additional dispatch based on cherrypy.request.method.upper().
    
    Methods named GET, POST, etc will be called on an exposed class.
    The method names must be all caps; the appropriate Allow header
    will be output showing all capitalized method names as allowable
    HTTP verbs.
    
    Note that the containing class must be exposed, not the methods.
    """
    
    def __call__(self, path_info):
        """Set handler and config for the current request."""
        request = cherrypy.request
        resource, vpath = self.find_handler(path_info)
        
        if resource:
            # Set Allow header
            avail = [m for m in dir(resource) if m.isupper()]
            if "GET" in avail and "HEAD" not in avail:
                avail.append("HEAD")
            avail.sort()
            cherrypy.response.headers['Allow'] = ", ".join(avail)
            
            # Find the subhandler
            meth = request.method.upper()
            func = getattr(resource, meth, None)
            if func is None and meth == "HEAD":
                func = getattr(resource, "GET", None)
            if func:
                # Decode any leftover %2F in the virtual_path atoms.
                vpath = [x.replace("%2F", "/") for x in vpath]
                request.handler = LateParamPageHandler(func, *vpath)
            else:
                request.handler = cherrypy.HTTPError(405)
        else:
            request.handler = cherrypy.NotFound()


class WSGIEnvProxy(object):
    
    def __getattr__(self, key, default=None):
        return getattr(cherrypy.request.wsgi_environ, key, default)


class RoutesDispatcher(object):
    """A Routes based dispatcher for CherryPy."""
    
    def __init__(self):
        import routes
        self.controllers = {}
        self.mapper = routes.Mapper()
        self.mapper.controller_scan = self.controllers.keys
        
        # Since Routes' mapper.environ is not threadsafe,
        # we must use a proxy which does JIT lookup.
        self.mapper.environ = WSGIEnvProxy()
    
    def connect(self, name, route, controller, **kwargs):
        self.controllers[name] = controller
        self.mapper.connect(name, route, controller=name, **kwargs)
    
    def redirect(self, url):
        raise cherrypy.HTTPRedirect(url)
    
    def __call__(self, path_info):
        """Set handler and config for the current request."""
        func = self.find_handler(path_info)
        if func:
            cherrypy.request.handler = LateParamPageHandler(func)
        else:
            cherrypy.request.handler = cherrypy.NotFound()
    
    def find_handler(self, path_info):
        """Find the right page handler, and set request.config."""
        import routes
        
        request = cherrypy.request
        
        config = routes.request_config()
        config.mapper = self.mapper
        config.host = request.headers.get('Host', None)
        config.protocol = request.scheme
        config.redirect = self.redirect
        
        result = self.mapper.match(path_info)
        config.mapper_dict = result
        request.params.update(result or {})
        
        # Get config for the root object/path.
        request.config = base = cherrypy.config.copy()
        curpath = ""
        
        def merge(nodeconf):
            if 'tools.staticdir.dir' in nodeconf:
                nodeconf['tools.staticdir.section'] = curpath or "/"
            base.update(nodeconf)
        
        app = request.app
        root = app.root
        if hasattr(root, "_cp_config"):
            merge(root._cp_config)
        if "/" in app.config:
            merge(app.config["/"])
        
        # Mix in values from app.config.
        atoms = [x for x in path_info.split("/") if x]
        if atoms:
            last = atoms.pop()
        else:
            last = None
        for atom in atoms:
            curpath = "/".join((curpath, name))
            if curpath in app.config:
                merge(app.config[curpath])
        
        handler = None
        if result:
            controller = result.get('controller', None)
            controller = self.controllers.get(controller)
            if controller:
                # Get config from the controller.
                if hasattr(controller, "_cp_config"):
                    merge(controller._cp_config)
            
            action = result.get('action', None)
            if action is not None:
                handler = getattr(controller, action)
        
        # Do the last path atom here so it can
        # override the controller's _cp_config.
        if last:
            curpath = "/".join((curpath, last))
            if curpath in app.config:
                merge(app.config[curpath])
        
        return handler