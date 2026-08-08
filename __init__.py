def classFactory(iface):
    from .granulometria_plugin import GranulometriaPlugin
    return GranulometriaPlugin(iface)
