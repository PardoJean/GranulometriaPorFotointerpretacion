# CLAUDE.md — Regla de oro de esta carpeta

Este directorio (`GranulometriaPorFotointerpretacion\`) es el **paquete público** del plugin,
con su propio repo git (`origin` → `github.com/PardoJean/GranulometriaPorFotointerpretacion`) y
versionado semver independiente (`1.x.x`).

**Trabajar únicamente dentro de esta carpeta.** No editar, leer para modificar, ni empaquetar
nada de:

- `..\GranulometriaPorFotointerpretacion V3\` — versión interna 3.x que usa el laboratorio a
  diario. Es una copia hermana **sin git**, de-brandeada aquí (sin logos ni menciones a
  ECUACORRIENTE/ECSA/GDR). Portar un cambio de esta carpeta a la V3, o viceversa, es una decisión
  de producto aparte que hay que confirmar con el usuario — nunca un default automático.
- `..\GranulometriaPorFotointerpretacion V1\` — versión vieja, no se toca.
- `..\Procedimiento\` — documentación oficial ECSA, entregable independiente.

Reglas específicas de este paquete (heredadas del `CLAUDE.md` padre, sección 10):

- Sin `openpyxl` (arrastra `lxml`, choca con GDAL/QGIS → *access violation* que tumba QGIS).
- Nunca `except Exception: pass` — dispara Bandit B110 y bloquea la versión en
  plugins.qgis.org de forma **permanente**. Usar el callback `log(msg)` existente.
- Enums de Qt siempre calificados (`Qt.AlignmentFlag.*`, `QDialog.DialogCode.*`,
  `QgsWkbTypes.GeometryType.*`, etc.), imports vía `qgis.PyQt.*` (no `PyQt5.*` directo),
  `.exec()` en vez de `.exec_()`.
- Empaquetar el ZIP solo con `zipfile` de Python — nunca con
  `[System.IO.Compression.ZipFile]::CreateFromDirectory` de PowerShell (genera `\` en los
  nombres de entrada y el validador de plugins.qgis.org rechaza el ZIP entero).
- Verificar antes de subir versión: `python -m bandit -r "GranulometriaPorFotointerpretacion"`
  debe dar "No issues identified".
