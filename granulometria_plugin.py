# -*- coding: utf-8 -*-
"""
Granulometria por Fotointerpretacion
-------------------------------------------------------------------------------
Measures rock grain sizes in a georeferenced photo (photointerpretation) and
exports gradation reports (Excel + GeoPackage).

Copyright (C) 2026 Jean Pardo

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
-------------------------------------------------------------------------------
"""

import os
import math

from qgis.core import (
    QgsField, QgsProject, QgsGeometry, QgsWkbTypes, QgsVectorLayer, QgsFeature,
    QgsSingleSymbolRenderer, QgsFillSymbol, QgsRasterLayer,
    QgsVectorFileWriter, QgsCoordinateTransform, Qgis
)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
from qgis.PyQt.QtCore import QVariant, Qt, QSettings
from qgis.PyQt.QtWidgets import (
    QAction, QComboBox, QDialog, QVBoxLayout, QLabel, QPushButton, QMessageBox,
    QFileDialog, QFormLayout, QHBoxLayout, QProgressBar, QGroupBox,
    QGridLayout, QLineEdit, QCheckBox, QDialogButtonBox, QPlainTextEdit
)
from qgis.PyQt.QtGui import QIcon, QColor

# ===========================================================================
# TAMICES ESTÁNDAR ASTM (nombre, apertura en metros)
# ===========================================================================
TAMICES_STD = [
    ('6"',   0.15240),
    ('4"',   0.10160),
    ('3"',   0.07620),
    ('2"',   0.05080),
    ('1½"',  0.03810),
    ('1"',   0.02540),
    ('¾"',   0.01905),
    ('½"',   0.01270),
    ('⅜"',   0.00953),
]

# ===========================================================================
# CLASE HERRAMIENTA DE DIBUJO
# ===========================================================================

class PolygonMapTool(QgsMapToolEmitPoint):
    def __init__(self, iface, on_polygon_drawn):
        super().__init__(iface.mapCanvas())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.on_polygon_drawn = on_polygon_drawn
        self.points = []
        self.rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.rubber_band.setColor(QColor(255, 0, 0, 100))
        self.rubber_band.setWidth(2)

    def canvasPressEvent(self, e):
        if e.button() == 1:
            self.points.append(self.toMapCoordinates(e.pos()))
            self.update_rubber_band()
        elif e.button() == 2:
            self.finalize_polygon()

    def canvasMoveEvent(self, e):
        if self.points:
            self.update_rubber_band(self.toMapCoordinates(e.pos()))

    def update_rubber_band(self, temporary_point=None):
        pts = list(self.points)
        if temporary_point:
            pts.append(temporary_point)
        if len(pts) > 1:
            geom = (QgsGeometry.fromPolylineXY(pts) if len(pts) < 3
                    else QgsGeometry.fromPolygonXY([pts]))
            self.rubber_band.setToGeometry(geom, None)

    def finalize_polygon(self):
        if len(self.points) > 2:
            self.on_polygon_drawn(QgsGeometry.fromPolygonXY([self.points]))
            self.iface.mapCanvas().unsetMapTool(self)

    def deactivate(self):
        self.rubber_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self.points = []
        super().deactivate()


# ===========================================================================
# FUNCIONES DE CAPA
# ===========================================================================

def get_polygon_layers():
    return [lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer)
            and lyr.geometryType() == QgsWkbTypes.GeometryType.PolygonGeometry]


def get_raster_layers():
    return [lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsRasterLayer)]


def clean_fields(layer):
    to_remove = [n for n in ['Area_1', 'Area_2', 'Area_3']
                 if n in [f.name() for f in layer.fields()]]
    if to_remove:
        layer.startEditing()
        idxs = [layer.fields().indexFromName(n) for n in to_remove]
        layer.dataProvider().deleteAttributes(idxs)
        layer.updateFields()
        layer.commitChanges()


def process_polygons(layer, progress_bar=None, umbral_pulgadas=0.75, area_total_geom=None):
    """Procesa los polígonos de granos. El área total es el contorno de la foto
    (area_total_geom); el área de material grueso es la suma de los polígonos
    que superan el umbral; el área de finos surge por diferencia."""
    required = {
        'Numero': QVariant.Int, 'Area': QVariant.Double,
        'Diametro_p': QVariant.Double, 'Pulgada': QVariant.String,
        'area_total': QVariant.Double
    }
    existing = [f.name() for f in layer.fields()]
    to_add = [QgsField(n, t) for n, t in required.items() if n not in existing]
    if to_add:
        layer.startEditing()
        layer.dataProvider().addAttributes(to_add)
        layer.commitChanges()
        layer.updateFields()

    idx_num = layer.fields().indexFromName('Numero')
    idx_area = layer.fields().indexFromName('Area')
    idx_diam = layer.fields().indexFromName('Diametro_p')
    idx_inch = layer.fields().indexFromName('Pulgada')
    idx_tot = layer.fields().indexFromName('area_total')

    total_area = 0.0
    if area_total_geom is not None and not area_total_geom.isEmpty():
        total_area = area_total_geom.area()
    total_area = round(total_area, 4)

    irregular_count = small_count = initial_count = 0
    area_gruesos = 0.0
    number = 1
    features = list(layer.getFeatures())
    n = len(features)
    ids_delete = []

    layer.startEditing()
    for i, feat in enumerate(features):
        if progress_bar:
            progress_bar.setValue(int((i + 1) / n * 100))
        geom = feat.geometry()
        initial_count += 1

        if geom.isNull() or geom.isEmpty():
            for idx in [idx_diam, idx_num, idx_area, idx_inch, idx_tot]:
                layer.changeAttributeValue(feat.id(), idx, None)
            irregular_count += 1
            continue

        try:
            mw = geom.minimumWidth()
            min_d = mw.length() if not mw.isNull() else 0
        except Exception:
            min_d = 0

        if min_d <= 0:
            layer.changeAttributeValue(feat.id(), idx_diam, None)
            layer.changeAttributeValue(feat.id(), idx_inch, None)
            layer.changeAttributeValue(feat.id(), idx_tot, total_area)
            irregular_count += 1
            continue

        min_d_inch = min_d * 39.3701
        area = geom.area()
        bbox = geom.boundingBox()
        w, h = bbox.width(), bbox.height()
        aspect = max(w, h) / min(w, h) if min(w, h) > 0 else 1
        circ_area = math.pi * (min_d / 2) ** 2
        ratio_circ = area / circ_area if circ_area > 0 else 0
        bbox_area = w * h
        ratio_bbox = area / bbox_area if bbox_area > 0 else 0

        if (aspect <= 3) or (0.7 <= ratio_circ <= 1.3) or (ratio_bbox >= 0.7):
            final_d = round(min_d, 2)
        else:
            final_d = round(max(w, h), 2)
            irregular_count += 1

        layer.changeAttributeValue(feat.id(), idx_num, number)
        layer.changeAttributeValue(feat.id(), idx_area, round(area, 4))
        layer.changeAttributeValue(feat.id(), idx_diam, final_d)
        layer.changeAttributeValue(feat.id(), idx_tot, total_area)

        if min_d_inch < umbral_pulgadas:
            layer.changeAttributeValue(feat.id(), idx_inch, f'< {umbral_pulgadas}"')
            small_count += 1
            ids_delete.append(feat.id())
        else:
            layer.changeAttributeValue(feat.id(), idx_inch, str(round(min_d_inch, 2)))
            area_gruesos += area
            number += 1

    layer.commitChanges()

    if ids_delete:
        layer.startEditing()
        layer.dataProvider().deleteFeatures(ids_delete)
        layer.commitChanges()

    if progress_bar:
        progress_bar.setValue(100)

    area_gruesos = round(area_gruesos, 4)
    area_finos = round(max(0.0, total_area - area_gruesos), 4)
    pct_gruesos = round(area_gruesos / total_area * 100, 2) if total_area > 0 else 0.0
    pct_finos = round(area_finos / total_area * 100, 2) if total_area > 0 else 0.0
    inconsistente = area_gruesos > total_area

    return {
        'area_total': total_area,
        'area_gruesos': area_gruesos,
        'area_finos': area_finos,
        'pct_gruesos': pct_gruesos,
        'pct_finos': pct_finos,
        'inconsistente': inconsistente,
        'eliminados': small_count,
        'irregulares': irregular_count,
        'iniciales': initial_count,
        'finales': initial_count - small_count
    }


# ===========================================================================
# ANÁLISIS GRANULOMÉTRICO
# ===========================================================================

def calculate_gradation_curve(layer, total_area):
    """Calcula la curva granulométrica basada en áreas de partículas."""
    particles = []
    for feat in layer.getFeatures():
        d = feat['Diametro_p']
        a = feat['Area']
        if d is not None and a is not None:
            try:
                particles.append((float(d), float(a)))
            except (TypeError, ValueError):
                pass

    sum_remaining = sum(a for _, a in particles)
    fine_area = max(0.0, total_area - sum_remaining)
    pct_finos = round(fine_area / total_area * 100, 2) if total_area > 0 else 0.0

    gradation = []
    for sieve_name, sieve_m in TAMICES_STD:
        area_passing = fine_area + sum(a for d, a in particles if d <= sieve_m)
        pct = min(100.0, round(area_passing / total_area * 100, 2)) if total_area > 0 else 0.0
        gradation.append({
            'tamiz': sieve_name,
            'apertura_m': sieve_m,
            'apertura_mm': round(sieve_m * 1000, 3),
            'area_pasa': round(area_passing, 4),
            'pct_pasa': pct
        })

    return gradation, fine_area, pct_finos


def calculate_D_params(gradation):
    """Calcula D10, D30, D60, Cu, Cc e interpreta SUCS preliminar."""
    sorted_g = sorted(gradation, key=lambda x: x['apertura_m'])

    def interpolate(pct_target):
        for i in range(len(sorted_g) - 1):
            p1, p2 = sorted_g[i]['pct_pasa'], sorted_g[i + 1]['pct_pasa']
            d1, d2 = sorted_g[i]['apertura_m'], sorted_g[i + 1]['apertura_m']
            if p1 <= pct_target <= p2 and p2 > p1 and d1 > 0 and d2 > 0:
                log_d = (math.log10(d1)
                         + (pct_target - p1) / (p2 - p1)
                         * (math.log10(d2) - math.log10(d1)))
                return 10 ** log_d
        return None

    D10 = interpolate(10)
    D30 = interpolate(30)
    D60 = interpolate(60)

    Cu = round(D60 / D10, 2) if D10 and D60 and D10 > 0 else None
    Cc = round(D30 ** 2 / (D10 * D60), 2) if D10 and D30 and D60 and D10 > 0 and D60 > 0 else None

    sucs = '—'
    if Cu is not None and Cc is not None:
        sucs = 'GW (Grava bien gradada)' if Cu >= 4 and 1 <= Cc <= 3 else 'GP (Grava mal gradada)'

    return {
        'D10_mm': round(D10 * 1000, 2) if D10 else None,
        'D30_mm': round(D30 * 1000, 2) if D30 else None,
        'D60_mm': round(D60 * 1000, 2) if D60 else None,
        'Cu': Cu,
        'Cc': Cc,
        'sucs': sucs
    }


# ===========================================================================
# EXPORTACIÓN — EXCEL (formato plantilla GFI, limpio, sin gráficos)
#
# Escritor .xlsx propio, sin openpyxl. En la máquina del usuario openpyxl usa
# lxml (5.3.0) para construir el XML, y esa lxml trae su propia libxml2 que
# choca con la que ya tiene cargada QGIS/GDAL en el mismo proceso: la llamada
# a Element() termina en un access violation nativo (xmlDictReference) que
# tumba QGIS entero — no es una excepción de Python, así que ningún
# try/except lo detiene. Escribir el .xlsx a mano con zipfile/xml de la
# librería estándar elimina esa dependencia y el riesgo por completo.
# ===========================================================================

def _safe_num(value, decimals=4):
    try:
        return round(float(value), decimals)
    except (TypeError, ValueError):
        return value


def _xlsx_escape(text):
    return (str(text)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


def _col_letter(idx):
    """1 -> A, 2 -> B, ... (suficiente para las 2 columnas que usa el plugin)."""
    letters = ''
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


_XLSX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
    'spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.'
    'spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
    'spreadsheetml.styles+xml"/>'
    '</Types>'
)

_XLSX_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'officeDocument" Target="xl/workbook.xml"/>'
    '</Relationships>'
)

_XLSX_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
    '</workbook>'
)

_XLSX_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'worksheet" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'styles" Target="styles.xml"/>'
    '</Relationships>'
)

# Fuente Aptos Narrow 11 y borde fino en las cuatro aristas: mismo estilo que
# plantilla_gfi.xlsx. Todas las celdas usan s="1" (xf índice 1, con borde).
_XLSX_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="1"><font><sz val="11"/><name val="Aptos Narrow"/><family val="2"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="2">'
    '<border><left/><right/><top/><bottom/><diagonal/></border>'
    '<border>'
    '<left style="thin"><color indexed="64"/></left>'
    '<right style="thin"><color indexed="64"/></right>'
    '<top style="thin"><color indexed="64"/></top>'
    '<bottom style="thin"><color indexed="64"/></bottom>'
    '<diagonal/></border>'
    '</borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>'
)


def _xlsx_cell_xml(ref, value):
    """Una celda con estilo s="1" (borde fino, Aptos Narrow). None -> celda vacía."""
    if value is None:
        return f'<c r="{ref}" s="1"/>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}" s="1"><v>{value}</v></c>'
    text = _xlsx_escape(value)
    return f'<c r="{ref}" s="1" t="inlineStr"><is><t>{text}</t></is></c>'


def write_xlsx_plantilla(filepath, headers, rows):
    """Escribe un .xlsx de una sola hoja con el formato exacto de
    plantilla_gfi.xlsx: encabezado en la fila 1, datos desde la fila 2,
    borde fino y fuente Aptos Narrow en toda celda. Sin openpyxl."""
    import zipfile

    n_cols = len(headers)
    last_col = _col_letter(n_cols)
    last_row = 1 + len(rows)

    sheet_rows = []
    header_cells = ''.join(
        _xlsx_cell_xml(f'{_col_letter(c)}1', h) for c, h in enumerate(headers, start=1)
    )
    sheet_rows.append(f'<row r="1">{header_cells}</row>')

    for r, row_vals in enumerate(rows, start=2):
        cells = ''.join(
            _xlsx_cell_xml(f'{_col_letter(c)}{r}', v) for c, v in enumerate(row_vals, start=1)
        )
        sheet_rows.append(f'<row r="{r}">{cells}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_col}{last_row}"/>'
        '<sheetData>' + ''.join(sheet_rows) + '</sheetData>'
        '</worksheet>'
    )

    try:
        with zipfile.ZipFile(filepath, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('[Content_Types].xml', _XLSX_CONTENT_TYPES)
            z.writestr('_rels/.rels', _XLSX_RELS)
            z.writestr('xl/workbook.xml', _XLSX_WORKBOOK)
            z.writestr('xl/_rels/workbook.xml.rels', _XLSX_WORKBOOK_RELS)
            z.writestr('xl/styles.xml', _XLSX_STYLES)
            z.writestr('xl/worksheets/sheet1.xml', sheet_xml)
        return filepath
    except Exception as e:
        QMessageBox.critical(None, "Error al Guardar", f"No se pudo guardar el archivo:\n{e}")
        return None


def export_gfi(layer, filepath):
    """Excel limpio con el formato único de plantilla_gfi.xlsx: una sola hoja,
    columnas Área (m²) y Diámetro (m) — misma unidad base. Sin colores, sin gráficos."""
    fnames = [f.name() for f in layer.fields()]
    rows = []
    for feat in layer.getFeatures():
        area = feat['Area'] if 'Area' in fnames else None
        diam = feat['Diametro_p'] if 'Diametro_p' in fnames else None
        area_val = _safe_num(area, 4) if area is not None else None
        diam_val = _safe_num(diam, 4) if diam is not None else None
        rows.append([area_val, diam_val])

    return write_xlsx_plantilla(filepath, ['Área', 'Diámetro'], rows)


def export_info(resumen, img_info, filepath):
    """Excel limpio con la información complementaria: tamaño de imagen,
    área de gruesos y área de finos. Sin curvas ni parámetros SUCS."""
    rows = [
        ['Tamaño de imagen (px)', img_info.get('dims_px', '—')],
        ['Tamaño de imagen (MP)', img_info.get('mp', '—')],
        ['Área total (m²)', resumen.get('area_total', '—')],
        ['Área material grueso (m²)', resumen.get('area_gruesos', '—')],
        ['Área material fino (m²)', resumen.get('area_finos', '—')],
    ]
    return write_xlsx_plantilla(filepath, ['Parámetro', 'Valor'], rows)


# ===========================================================================
# EXPORTACIÓN — GEOPACKAGE (capa + foto + proyecto, reproyectado)
# ===========================================================================

def _gfi_outline_symbol():
    """Relleno transparente, borde rojo: solo se ven los contornos de los
    granos sobre la foto — para la capa de polígonos exportada al GeoPackage."""
    return QgsFillSymbol.createSimple({
        'color': '255,0,0,0',
        'outline_color': '255,0,0,255',
        'outline_width': '0.6',
        'outline_width_unit': 'MM',
    })


def export_geopackage(poly_layer, raster_layer, filepath, log_fn=None):
    """Genera un único GeoPackage autocontenido, SIN reproyectar nada:
      1) capa de polígonos, en su propio CRS,
      2) foto incrustada y comprimida (JPEG, con fallback a PNG) con pirámides,
         copiada tal cual del archivo fuente,
      3) simbología de la capa vectorial,
      4) un proyecto QGIS independiente embebido en el propio gpkg.
    Devuelve (ok_vector, ok_raster, ok_proyecto) para reportar fallos parciales.

    No se reproyecta ninguna capa: se exportan con las coordenadas que ya
    tienen en el proyecto. En fotos de laboratorio georreferenciadas solo
    para tener escala métrica (no una ubicación geográfica real), forzar una
    reproyección de datum no tiene sentido y fue la causa de los problemas
    de alineación/transparencia de versiones anteriores. Al no transformar
    nada, ráster y polígonos quedan garantizados en el mismo lugar relativo
    en el que ya se ven dentro de QGIS.
    """
    def log(msg):
        if log_fn:
            log_fn(msg)

    if os.path.exists(filepath):
        try:
            os.remove(filepath)
        except Exception as e:
            log(f"⚠ No se pudo reemplazar el archivo existente: {e}")
            return False, False, False

    base_name = os.path.splitext(os.path.basename(filepath))[0]

    # ---- 1) Capa de polígonos, en su propio CRS, sin reproyectar ----
    ok_vector = False
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = base_name
    options.fileEncoding = "UTF-8"
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        poly_layer, filepath, QgsProject.instance().transformContext(), options
    )
    err_code = result[0] if isinstance(result, tuple) else result
    if err_code == QgsVectorFileWriter.WriterError.NoError:
        ok_vector = True
        log(f"✔ Capa de polígonos exportada como '{base_name}' (CRS {poly_layer.crs().authid()}).")
    else:
        log(f"✘ Error exportando la capa de polígonos: {result}")
        return False, False, False

    # ---- 2) Ráster incrustado y comprimido, copiado tal cual (sin reproyectar) ----
    # Se copia directo el archivo fuente al GeoPackage con gdal.Translate — la
    # misma ruta de escritura de teselas que ya se comprobó visible. Sin
    # srcSRS/dstSRS: no hay reproyección ni elección de transformación de
    # datum, así que no hay forma de que quede desalineado con el vector.
    ok_raster = False
    raster_table = f"{base_name}_foto"
    if raster_layer is not None:
        try:
            src_path = raster_layer.source().split('|')[0]
            from osgeo import gdal
            gdal.UseExceptions()

            def _copy_into_gpkg(tile_format):
                return gdal.Translate(
                    filepath, src_path, format="GPKG",
                    creationOptions=[
                        "APPEND_SUBDATASET=YES",
                        f"RASTER_TABLE={raster_table}",
                        f"TILE_FORMAT={tile_format}",
                    ] + (["QUALITY=85"] if tile_format == "JPEG" else [])
                )

            ds = None
            for tile_format in ("JPEG", "PNG"):
                try:
                    ds = _copy_into_gpkg(tile_format)
                except Exception:
                    ds = None
                if ds is not None:
                    break

            if ds is not None:
                try:
                    ds.BuildOverviews("AVERAGE", [2, 4, 8, 16])
                except Exception:
                    pass
                ds = None
                ok_raster = True
                log(f"✔ Foto incrustada en la tabla ráster '{raster_table}' (CRS {raster_layer.crs().authid()}).")
            else:
                log("✘ No se pudo copiar la foto dentro del GeoPackage.")
        except Exception as e:
            log(f"✘ Error incrustando la foto: {e}")
    else:
        log("— No se seleccionó ráster; el GeoPackage solo contiene la capa de polígonos.")

    # ---- 2.3) Verificación automática de superposición espacial ----
    if ok_vector and ok_raster:
        try:
            chk_vlyr = QgsVectorLayer(f"{filepath}|layername={base_name}", "chk_v", "ogr")
            chk_rlyr = QgsRasterLayer(f"GPKG:{filepath}:{raster_table}", "chk_r")
            if chk_vlyr.isValid() and chk_rlyr.isValid() and chk_vlyr.featureCount() > 0:
                v_ext = chk_vlyr.extent()
                r_ext = chk_rlyr.extent()
                if not r_ext.intersects(v_ext):
                    log("⚠ Las capas del GeoPackage no se superponen. "
                        "Revisa el CRS de origen de la foto y de la capa de polígonos.")
                else:
                    log("✔ Verificado: la capa de polígonos y la foto se superponen espacialmente.")
        except Exception as e:
            log(f"⚠ No se pudo verificar la superposición: {e}")

    # ---- 3) Simbología de la capa vectorial ----
    # QGIS 3.42 cambió la firma de saveStyleToDatabase: devuelve un único str
    # (mensaje de error; vacío = éxito), no una tupla (str, bool) como en
    # versiones anteriores. Desempaquetarlo como tupla lanzaba ValueError,
    # capturado en silencio, y por eso nunca se guardaba la simbología.
    try:
        gpkg_vlayer = QgsVectorLayer(f"{filepath}|layername={base_name}", base_name, "ogr")
        if gpkg_vlayer.isValid():
            gpkg_vlayer.setRenderer(QgsSingleSymbolRenderer(_gfi_outline_symbol()))
            err_msg = gpkg_vlayer.saveStyleToDatabase("default", "Estilo GFI", True, "")
            if not err_msg:
                log("✔ Simbología guardada en el GeoPackage.")
            else:
                log(f"⚠ No se pudo guardar la simbología: {err_msg}")
    except Exception as e:
        log(f"⚠ No se pudo guardar la simbología: {e}")

    # ---- 4) Proyecto QGIS independiente embebido ----
    ok_proyecto = False
    try:
        proj = QgsProject()
        vlyr = QgsVectorLayer(f"{filepath}|layername={base_name}", base_name, "ogr")
        added = []
        if vlyr.isValid():
            vlyr.setRenderer(QgsSingleSymbolRenderer(_gfi_outline_symbol()))
            added.append(vlyr)
        if ok_raster:
            rlyr = QgsRasterLayer(f"GPKG:{filepath}:{raster_table}", raster_table)
            if rlyr.isValid():
                added.append(rlyr)
        if added:
            proj.addMapLayers(added)
            proj.setCrs(poly_layer.crs())
            ok_proyecto = proj.write(f"geopackage:{filepath}?projectName={base_name}")
            if ok_proyecto:
                log("✔ Proyecto QGIS embebido en el GeoPackage.")
            else:
                log("✘ No se pudo escribir el proyecto dentro del GeoPackage.")
    except Exception as e:
        log(f"✘ Error generando el proyecto embebido: {e}")

    return ok_vector, ok_raster, ok_proyecto


# ===========================================================================
# DIÁLOGOS
# ===========================================================================

def show_results_dialog(resumen, params, umbral_pulgadas):
    """Diálogo de resultados: áreas total/grueso/fino + parámetros geotécnicos."""
    dlg = QDialog()
    dlg.setWindowTitle("Resultados del Análisis Granulométrico")
    dlg.setMinimumWidth(440)
    dlg.setStyleSheet("""
        QDialog { background-color: #f7f7f7; font-family: Arial; }
        QGroupBox { background: white; border: 1px solid #ddd; border-radius: 6px;
                    margin-top: 1ex; font-weight: bold; color: #333; }
        QGroupBox::title { subcontrol-origin: margin; padding: 0 8px; margin-left: 8px;
                           background: #f7f7f7; }
        QLabel { color: #444; font-size: 10pt; }
    """)

    layout = QVBoxLayout(dlg)
    layout.setSpacing(10)
    layout.setContentsMargins(14, 14, 14, 14)

    if resumen.get('inconsistente'):
        warn = QLabel("⚠ El área de material grueso supera el área total dibujada.\n"
                       "Revisa el contorno del área total.")
        warn.setStyleSheet("color: #c0392b; font-weight: bold;")
        warn.setWordWrap(True)
        layout.addWidget(warn)

    # --- Resumen de áreas ---
    grp0 = QGroupBox("📐 Áreas")
    g0 = QGridLayout(grp0)
    g0.setVerticalSpacing(4)
    area_data = [
        ("Área total:", f"{resumen['area_total']:.4f} m²"),
        ("Área material grueso:", f"{resumen['area_gruesos']:.4f} m²  ({resumen['pct_gruesos']:.2f} %)"),
        ("Área material fino:", f"{resumen['area_finos']:.4f} m²  ({resumen['pct_finos']:.2f} %)"),
    ]
    for i, (k, v) in enumerate(area_data):
        lbl_k = QLabel(k); lbl_k.setStyleSheet("font-weight: bold; color: #555;")
        lbl_v = QLabel(v); lbl_v.setStyleSheet("color: #0073e6; font-weight: bold;")
        lbl_v.setAlignment(Qt.AlignmentFlag.AlignRight)
        g0.addWidget(lbl_k, i, 0)
        g0.addWidget(lbl_v, i, 1)
    layout.addWidget(grp0)

    # --- Resumen procesamiento ---
    grp1 = QGroupBox("📊 Resumen de Procesamiento")
    g1 = QGridLayout(grp1)
    g1.setVerticalSpacing(4)
    proc_data = [
        ("Polígonos iniciales:", str(resumen['iniciales'])),
        (f"Eliminados (< {umbral_pulgadas}\"):", str(resumen['eliminados'])),
        ("Polígonos finales:", str(resumen['finales'])),
        ("Polígonos irregulares:", str(resumen['irregulares'])),
    ]
    for i, (k, v) in enumerate(proc_data):
        lbl_k = QLabel(k); lbl_k.setStyleSheet("font-weight: bold; color: #555;")
        lbl_v = QLabel(v); lbl_v.setStyleSheet("color: #0073e6; font-weight: bold;")
        lbl_v.setAlignment(Qt.AlignmentFlag.AlignRight)
        g1.addWidget(lbl_k, i, 0)
        g1.addWidget(lbl_v, i, 1)
    layout.addWidget(grp1)

    # --- Parámetros geotécnicos (solo en pantalla) ---
    grp2 = QGroupBox("📈 Parámetros Granulométricos")
    g2 = QGridLayout(grp2)
    g2.setVerticalSpacing(4)

    def fmt(val, unit='mm'):
        return f"{val} {unit}" if val is not None else "Fuera de rango"

    geo_data = [
        ("D10:", fmt(params.get('D10_mm'))),
        ("D30:", fmt(params.get('D30_mm'))),
        ("D60:", fmt(params.get('D60_mm'))),
        ("Cu (Coef. Uniformidad):", fmt(params.get('Cu'), '')),
        ("Cc (Coef. Curvatura):", fmt(params.get('Cc'), '')),
        ("Clasificación SUCS:", params.get('sucs', '—')),
    ]
    for i, (k, v) in enumerate(geo_data):
        lk = QLabel(k); lk.setStyleSheet("font-weight: bold; color: #555;")
        lv = QLabel(v)
        color = "#27ae60" if "GW" in v else ("#e74c3c" if "GP" in v else "#0073e6")
        lv.setStyleSheet(f"color: {color}; font-weight: bold;")
        lv.setAlignment(Qt.AlignmentFlag.AlignRight)
        g2.addWidget(lk, i, 0)
        g2.addWidget(lv, i, 1)
    layout.addWidget(grp2)

    btn = QPushButton("Aceptar")
    btn.setObjectName("primary_button")
    btn.setStyleSheet("background:#0073e6; color:white; border-radius:4px; "
                      "padding:7px 20px; font-weight:bold; font-size:10pt;")
    btn.clicked.connect(dlg.accept)
    h = QHBoxLayout(); h.addStretch(); h.addWidget(btn); layout.addLayout(h)

    dlg.exec()


def parse_inches(value_str):
    """Convierte string de pulgadas a float. Soporta enteros, fracciones ASCII y Unicode."""
    UNICODE_FRACS = {
        '½': '1/2', '⅓': '1/3', '⅔': '2/3', '¼': '1/4', '¾': '3/4',
        '⅛': '1/8', '⅜': '3/8', '⅝': '5/8', '⅞': '7/8',
    }
    value_str = value_str.strip().replace('"', '')
    for uc, asc in UNICODE_FRACS.items():
        value_str = value_str.replace(uc, asc)
    value_str = value_str.strip()
    if ' ' in value_str:
        parts = value_str.split(' ', 1)
        entero = float(parts[0])
        num, den = parts[1].split('/')
        return entero + float(num) / float(den)
    elif '/' in value_str:
        num, den = value_str.split('/')
        return float(num) / float(den)
    return float(value_str)


PLUGIN_VERSION = "1.0.0"
PLUGIN_FECHA = "2026-08-08"

ACERCA_DE_QUE_HACE = (
    "Este plugin mide el tamaño de los granos de roca en una foto y calcula qué "
    "porcentaje del área es material grueso y qué porcentaje es material fino.\n\n"
    "1) Dibujas o generas el contorno de toda la foto: esa es el área total.\n"
    "2) El plugin mide cada polígono (grano) que hayas segmentado y descarta los "
    "más pequeños que el tamiz que elijas.\n"
    "3) El área de los granos que quedan (≥ tamiz) es el material grueso.\n"
    "4) El área total menos el área de gruesos es el material fino."
)

ACERCA_DE_NOVEDADES = (
    "Versión 1.0.0:\n"
    "  • Primera versión pública del plugin."
)


def show_about_dialog(parent=None):
    dlg = QDialog(parent)
    dlg.setWindowTitle("Acerca de — Granulometría por Fotointerpretación")
    dlg.setMinimumWidth(480)
    dlg.setStyleSheet("""
        QDialog { background-color: #f7f7f7; font-family: Arial; }
        QGroupBox { background: white; border: 1px solid #ddd; border-radius: 6px;
                    margin-top: 1ex; font-weight: bold; color: #333; }
        QGroupBox::title { subcontrol-origin: margin; padding: 0 8px; margin-left: 8px;
                           background: #f7f7f7; }
        QLabel { color: #444; font-size: 10pt; }
    """)
    layout = QVBoxLayout(dlg)
    layout.setSpacing(10)
    layout.setContentsMargins(14, 14, 14, 14)

    title = QLabel("Análisis Granulométrico por Fotointerpretación")
    title.setStyleSheet("font-size: 12pt; font-weight: bold; color: #1a4a7a;")
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(title)

    grp1 = QGroupBox("¿Qué hace?")
    l1 = QVBoxLayout(grp1)
    lbl1 = QLabel(ACERCA_DE_QUE_HACE)
    lbl1.setWordWrap(True)
    l1.addWidget(lbl1)
    layout.addWidget(grp1)

    grp2 = QGroupBox("Novedades de esta versión")
    l2 = QVBoxLayout(grp2)
    lbl2 = QLabel(ACERCA_DE_NOVEDADES)
    lbl2.setWordWrap(True)
    l2.addWidget(lbl2)
    layout.addWidget(grp2)

    lbl_meta = QLabel(f"Versión {PLUGIN_VERSION}  ·  {PLUGIN_FECHA}")
    lbl_meta.setStyleSheet("color: #888; font-size: 9pt;")
    lbl_meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(lbl_meta)

    btn = QPushButton("Cerrar")
    btn.setStyleSheet("background:#0073e6; color:white; border-radius:4px; "
                      "padding:7px 20px; font-weight:bold; font-size:10pt;")
    btn.clicked.connect(dlg.accept)
    h = QHBoxLayout(); h.addStretch(); h.addWidget(btn); layout.addLayout(h)

    dlg.exec()


# ===========================================================================
# DIÁLOGO DE EXPORTACIÓN
# ===========================================================================

class ExportDialog(QDialog):
    """Ventana única para exportar: Excel Área/Diámetro (plantilla GFI),
    Excel de Información complementaria y GeoPackage (capa + foto + proyecto)."""

    def __init__(self, default_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Exportar Todo")
        self.setMinimumWidth(480)
        self.setStyleSheet("""
            QDialog { background-color: #f7f7f7; font-family: Arial; }
            QGroupBox { background: white; border: 1px solid #ddd; border-radius: 6px;
                        margin-top: 1ex; font-weight: bold; color: #333; }
            QGroupBox::title { subcontrol-origin: margin; padding: 0 8px; margin-left: 8px;
                               background: #f7f7f7; }
            QLabel { color: #444; font-size: 10pt; }
            QLineEdit, QPlainTextEdit { background: white; border: 1px solid #ccc;
                        border-radius: 4px; padding: 4px; }
        """)

        settings = QSettings()
        last_folder = settings.value("GranulometriaGFI/ultima_carpeta", "")

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # --- Carpeta destino ---
        grp_folder = QGroupBox("Carpeta destino (servidor)")
        lf = QHBoxLayout(grp_folder)
        self.txt_folder = QLineEdit(last_folder)
        btn_browse = QPushButton("Examinar…")
        btn_browse.clicked.connect(self._browse_folder)
        lf.addWidget(self.txt_folder)
        lf.addWidget(btn_browse)
        layout.addWidget(grp_folder)

        # --- Nombre base ---
        grp_name = QGroupBox("Nombre base")
        ln = QVBoxLayout(grp_name)
        self.txt_name = QLineEdit(default_name)
        ln.addWidget(self.txt_name)
        layout.addWidget(grp_name)

        # --- Nota de coordenadas ---
        lbl_crs_info = QLabel("El GeoPackage se exporta con las coordenadas actuales "
                              "de cada capa (sin reproyectar).")
        lbl_crs_info.setWordWrap(True)
        lbl_crs_info.setStyleSheet("color: #888; font-size: 9pt;")
        layout.addWidget(lbl_crs_info)

        # --- Qué exportar ---
        grp_what = QGroupBox("Archivos a generar")
        lw = QVBoxLayout(grp_what)
        self.chk_gfi = QCheckBox("Excel Área y Diámetro (formato plantilla GFI)")
        self.chk_info = QCheckBox("Excel Información (tamaño de imagen, gruesos, finos)")
        self.chk_gpkg = QCheckBox("GeoPackage (capa + foto + proyecto)")
        self.chk_gfi.setChecked(True)
        self.chk_info.setChecked(True)
        self.chk_gpkg.setChecked(True)
        lw.addWidget(self.chk_gfi)
        lw.addWidget(self.chk_info)
        lw.addWidget(self.chk_gpkg)
        layout.addWidget(grp_what)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(110)
        self.log_box.setVisible(False)
        layout.addWidget(self.log_box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Exportar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Selecciona la carpeta destino")
        if folder:
            self.txt_folder.setText(folder)

    def log(self, msg):
        self.log_box.setVisible(True)
        self.log_box.appendPlainText(msg)

    def values(self):
        return {
            'folder': self.txt_folder.text().strip(),
            'name': self.txt_name.text().strip(),
            'do_gfi': self.chk_gfi.isChecked(),
            'do_info': self.chk_info.isChecked(),
            'do_gpkg': self.chk_gpkg.isChecked(),
        }


# ===========================================================================
# DIÁLOGO PRINCIPAL
# ===========================================================================

def main_dialog(iface):
    layers = get_polygon_layers()
    if not layers:
        iface.messageBar().pushMessage("Error", "No hay capas de polígono en el proyecto",
                                       level=3, duration=5)
        return

    dialog = QDialog()
    dialog.setWindowTitle("Análisis Granulométrico por Fotointerpretación")
    dialog.setMinimumWidth(540)

    STYLE = """
    QDialog { background-color: #f7f7f7; font-family: Arial, sans-serif; }
    QGroupBox { background-color: #ffffff; border: 1px solid #dddddd;
                border-radius: 8px; margin-top: 1ex; font-weight: bold; color: #333; }
    QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left;
                       padding: 0 10px; margin-left: 10px; background-color: #f7f7f7; }
    QLabel { color: #444444; font-size: 10pt; }
    QPushButton { background-color: #f0f0f0; color: #333333; border: 1px solid #cccccc;
                  border-radius: 4px; padding: 6px 12px; font-size: 10pt; font-weight: bold; }
    QPushButton:hover { background-color: #e0e0e0; border-color: #aaaaaa; }
    QPushButton:pressed { background-color: #d0d0d0; }
    QPushButton#primary_button { background-color: #0073e6; color: white; border: 1px solid #0073e6; }
    QPushButton#primary_button:hover { background-color: #005cb8; }
    QPushButton#primary_button:pressed { background-color: #004c99; }
    QPushButton:disabled { background-color: #e8e8e8; color: #aaaaaa; border-color: #dddddd; }
    QComboBox, QListWidget { background-color: white; border: 1px solid #cccccc;
                             border-radius: 4px; padding: 4px; font-size: 10pt; }
    QProgressBar { border: 1px solid #cccccc; border-radius: 4px; text-align: center; color: #333; }
    QProgressBar::chunk { background-color: #0073e6; border-radius: 3px; }
    """
    dialog.setStyleSheet(STYLE)

    # Estado del diálogo para compartir entre funciones
    dialog._resumen = None
    dialog._gradation = None
    dialog._params = None

    crs = QgsProject.instance().crs().authid()
    dialog.area_layer = QgsVectorLayer(f"Polygon?crs={crs}", "Área Total (Temporal)", "memory")
    QgsProject.instance().addMapLayer(dialog.area_layer)
    symbol = QgsFillSymbol.createSimple(
        {'color': '0,115,230,40', 'outline_color': '#0073e6', 'outline_width': '0.6'}
    )
    dialog.area_layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    dialog.map_tool = None

    layout = QVBoxLayout(dialog)
    layout.setSpacing(12)
    layout.setContentsMargins(15, 15, 15, 15)

    # --- Título ---
    title = QLabel("Análisis Granulométrico por Fotointerpretación")
    title.setStyleSheet("font-size: 14pt; font-weight: bold; color: #1a4a7a; margin-bottom: 4px;")
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(title)

    # --- Grupo 1: Capas de trabajo ---
    grp_capas = QGroupBox("1. Capas de Trabajo")
    lay_capas = QFormLayout(grp_capas)
    lay_capas.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
    combo_layer = QComboBox()
    for lyr in layers:
        combo_layer.addItem(lyr.name(), lyr.id())
    lay_capas.addRow("Capa de polígonos:", combo_layer)
    lbl_count = QLabel("—")
    lbl_count.setStyleSheet("color: #0073e6; font-weight: bold;")
    lay_capas.addRow("Polígonos en capa:", lbl_count)

    combo_raster = QComboBox()
    rasters = get_raster_layers()
    for r in rasters:
        combo_raster.addItem(r.name(), r.id())
    lay_capas.addRow("Foto (ráster):", combo_raster)
    layout.addWidget(grp_capas)

    # --- Grupo 2: Área total (contorno de la foto) ---
    grp_area = QGroupBox("2. Área Total (Contorno de la Foto)")
    lay_area = QVBoxLayout(grp_area)
    lay_area.setSpacing(8)
    hbtn = QHBoxLayout()
    btn_contorno_raster = QPushButton("🖼  Usar Contorno del Ráster")
    btn_dibujar = QPushButton("✏  Dibujar Contorno")
    btn_borrar = QPushButton("🗑  Borrar")
    hbtn.addWidget(btn_contorno_raster)
    hbtn.addWidget(btn_dibujar)
    hbtn.addWidget(btn_borrar)
    lbl_area_total = QLabel("Área total: — (genera o dibuja el contorno)")
    lbl_area_total.setStyleSheet("color: #0073e6; font-weight: bold;")
    lay_area.addLayout(hbtn)
    lay_area.addWidget(lbl_area_total)
    layout.addWidget(grp_area)

    # --- Grupo 3: Parámetros ---
    grp_params = QGroupBox("3. Parámetros de Análisis")
    lay_params = QFormLayout(grp_params)
    lay_params.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
    combo_umbral = QComboBox()
    combo_umbral.addItems(['4"', '3"', '2"', '1 1/2"', '1"', '3/4"', '1/2"', '3/8"'])
    combo_umbral.setCurrentText('3/4"')
    combo_umbral.setToolTip("Partículas menores a este diámetro serán eliminadas del análisis "
                             "y su área pasa a contabilizarse como material fino.")
    lay_params.addRow("Eliminar partículas menores a:", combo_umbral)
    layout.addWidget(grp_params)

    # --- Grupo: Datos para información ---
    grp_info = QGroupBox("Datos para Información")
    lay_info = QFormLayout(grp_info)
    lay_info.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
    lbl_dims = QLabel("—")
    lbl_mp = QLabel("—")
    lbl_mp.setStyleSheet("color: #0073e6; font-weight: bold;")
    lbl_pixel = QLabel("—")
    lay_info.addRow("Dimensiones (px):", lbl_dims)
    lay_info.addRow("Tamaño de imagen:", lbl_mp)
    lay_info.addRow("Tamaño de píxel (m):", lbl_pixel)
    layout.addWidget(grp_info)

    # --- Barra de progreso ---
    progress_bar = QProgressBar()
    progress_bar.setVisible(False)
    layout.addWidget(progress_bar)

    # --- Botones de acción ---
    hact = QHBoxLayout()
    btn_acerca_de = QPushButton("ℹ  Acerca de")
    hact.addWidget(btn_acerca_de)
    hact.addStretch()
    btn_procesar = QPushButton("▶  Procesar Capa")
    btn_procesar.setObjectName("primary_button")
    btn_exportar = QPushButton("💾  Exportar Todo")
    btn_exportar.setEnabled(False)
    hact.addWidget(btn_procesar)
    hact.addWidget(btn_exportar)
    layout.addLayout(hact)

    # ---- Actualizar contador de polígonos ----
    def update_poly_count():
        lid = combo_layer.currentData()
        if not lid:
            lbl_count.setText("—")
            return
        lyr = QgsProject.instance().mapLayer(lid)
        if lyr:
            lbl_count.setText(f"{lyr.featureCount()} polígonos")
        else:
            lbl_count.setText("—")

    combo_layer.currentIndexChanged.connect(update_poly_count)
    update_poly_count()

    # ---- Actualizar datos de imagen (MP) ----
    def update_image_info():
        rid = combo_raster.currentData()
        if not rid:
            lbl_dims.setText("—")
            lbl_mp.setText("—")
            lbl_pixel.setText("—")
            return
        rlyr = QgsProject.instance().mapLayer(rid)
        if not rlyr:
            lbl_dims.setText("—")
            lbl_mp.setText("—")
            lbl_pixel.setText("—")
            return
        w, h = rlyr.width(), rlyr.height()
        mp = (w * h) / 1_000_000
        lbl_dims.setText(f"{w} × {h} px")
        lbl_mp.setText(f"{mp:.2f} MP")
        lbl_pixel.setText(f"{rlyr.rasterUnitsPerPixelX():.5f}")

    combo_raster.currentIndexChanged.connect(update_image_info)
    update_image_info()

    # ---- Parsear umbral ----
    def get_umbral():
        raw = combo_umbral.currentText().replace('"', '').strip()
        return parse_inches(raw)

    # ---- Área total: helpers ----
    def set_area_geom(geom):
        dialog.area_layer.dataProvider().truncate()
        feat = QgsFeature(dialog.area_layer.fields())
        feat.setGeometry(geom)
        dialog.area_layer.dataProvider().addFeatures([feat])
        dialog.area_layer.updateExtents()
        iface.mapCanvas().refresh()
        area_m2 = geom.area()
        lbl_area_total.setText(f"Área total: {area_m2:.4f} m²")

    def on_drawing_complete(geom):
        dialog.show()
        set_area_geom(geom)
        iface.messageBar().pushMessage("Éxito", "Contorno de área total actualizado.",
                                       level=Qgis.MessageLevel.Success, duration=3)

    def on_dibujar():
        dialog.hide()
        dialog.map_tool = PolygonMapTool(iface, on_drawing_complete)
        iface.mapCanvas().setMapTool(dialog.map_tool)
        iface.messageBar().pushMessage("Herramienta Activada",
                                       "Dibuja el contorno del área total en el mapa. "
                                       "Clic derecho para finalizar.",
                                       duration=7)

    def on_usar_contorno_raster():
        rid = combo_raster.currentData()
        if not rid:
            QMessageBox.warning(dialog, "Advertencia", "Selecciona primero una capa ráster (foto).")
            return
        rlyr = QgsProject.instance().mapLayer(rid)
        if not rlyr:
            QMessageBox.critical(dialog, "Error", "No se pudo encontrar la capa ráster seleccionada.")
            return

        proj_crs = QgsProject.instance().crs()
        rect = rlyr.extent()
        if rlyr.crs() != proj_crs:
            tr = QgsCoordinateTransform(rlyr.crs(), proj_crs, QgsProject.instance())
            try:
                rect = tr.transformBoundingBox(rect)
            except Exception:
                QMessageBox.critical(dialog, "Error",
                                     "No se pudo reproyectar el contorno del ráster al CRS del proyecto.")
                return

        if proj_crs.isGeographic():
            QMessageBox.warning(dialog, "Aviso",
                                "El proyecto está en un CRS geográfico (grados). "
                                "Las áreas calculadas no estarán en m² reales; "
                                "usa un CRS proyectado (p. ej. UTM) para resultados correctos.")

        geom = QgsGeometry.fromRect(rect)
        set_area_geom(geom)
        iface.mapCanvas().refresh()

    def on_borrar_area():
        dialog.area_layer.dataProvider().truncate()
        dialog.area_layer.updateExtents()
        iface.mapCanvas().refresh()
        lbl_area_total.setText("Área total: — (genera o dibuja el contorno)")

    def get_area_total_geom():
        feats = list(dialog.area_layer.getFeatures())
        if not feats:
            return None
        return feats[0].geometry()

    # ---- Procesar ----
    def on_procesar():
        area_geom = get_area_total_geom()
        if area_geom is None or area_geom.isEmpty():
            QMessageBox.warning(dialog, "Advertencia",
                                "Genera o dibuja primero el área total (contorno de la foto).")
            return

        lid = combo_layer.currentData()
        if not lid:
            QMessageBox.warning(dialog, "Advertencia", "No has seleccionado una capa.")
            return
        lyr = QgsProject.instance().mapLayer(lid)
        if not lyr:
            QMessageBox.critical(dialog, "Error", "No se pudo encontrar la capa seleccionada.")
            return

        clean_fields(lyr)
        progress_bar.setVisible(True)
        progress_bar.setValue(0)
        btn_procesar.setEnabled(False)

        umbral = get_umbral()
        resumen = process_polygons(lyr, progress_bar, umbral, area_geom)
        gradation, fine_area, pct_finos = calculate_gradation_curve(lyr, resumen['area_total'])
        gparams = calculate_D_params(gradation) if gradation else {}

        dialog._resumen = resumen
        dialog._gradation = gradation
        dialog._params = gparams

        iface.showAttributeTable(lyr)
        progress_bar.setVisible(False)
        btn_procesar.setEnabled(True)
        btn_exportar.setEnabled(True)
        update_poly_count()

        show_results_dialog(resumen, gparams, umbral)

    # ---- Exportar todo ----
    def on_exportar():
        lid = combo_layer.currentData()
        if not lid:
            return
        lyr = QgsProject.instance().mapLayer(lid)
        if not lyr:
            return
        rid = combo_raster.currentData()
        rlyr = QgsProject.instance().mapLayer(rid) if rid else None

        exp_dlg = ExportDialog(default_name=lyr.name())
        if exp_dlg.exec() != QDialog.DialogCode.Accepted:
            return

        vals = exp_dlg.values()
        folder = vals['folder']
        name = vals['name']
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(dialog, "Advertencia", "Selecciona una carpeta destino válida.")
            return
        if not name:
            QMessageBox.warning(dialog, "Advertencia", "Ingresa un nombre base.")
            return

        settings = QSettings()
        settings.setValue("GranulometriaGFI/ultima_carpeta", folder)

        generated = []

        if vals['do_gfi']:
            path = os.path.join(folder, f"{name}_GFI.xlsx")
            r = export_gfi(lyr, path)
            if r:
                generated.append(r)

        if vals['do_info']:
            w = rlyr.width() if rlyr else None
            h = rlyr.height() if rlyr else None
            img_info = {
                'dims_px': f"{w} × {h}" if w and h else "—",
                'mp': f"{(w * h) / 1_000_000:.2f}" if w and h else "—",
            }
            path = os.path.join(folder, f"{name}_INFO.xlsx")
            r = export_info(dialog._resumen or {}, img_info, path)
            if r:
                generated.append(r)

        gpkg_log = []
        if vals['do_gpkg']:
            path = os.path.join(folder, f"{name}.gpkg")
            ok_v, ok_r, ok_p = export_geopackage(lyr, rlyr, path, log_fn=gpkg_log.append)
            if ok_v:
                generated.append(path)
            if not ok_r and rlyr is not None:
                QMessageBox.warning(dialog, "Aviso",
                                    "El GeoPackage se generó, pero no se pudo incrustar la foto.")
            if not ok_p:
                QMessageBox.warning(dialog, "Aviso",
                                    "El GeoPackage se generó, pero no se pudo embeber el proyecto.")
            problemas = [m for m in gpkg_log if m.startswith('⚠') or m.startswith('✘')]
            if problemas:
                QMessageBox.warning(dialog, "Aviso del GeoPackage", "\n".join(problemas))

        if generated:
            resumen_gpkg = ("\n\n" + "\n".join(gpkg_log)) if gpkg_log else ""
            QMessageBox.information(dialog, "Exportación completa",
                                    "✅ Archivos guardados en:\n" + folder + "\n\n" +
                                    "\n".join(f"  • {os.path.basename(p)}" for p in generated) +
                                    resumen_gpkg)
        else:
            QMessageBox.warning(dialog, "Advertencia", "No se generó ningún archivo.")

    def cleanup():
        if dialog.area_layer:
            QgsProject.instance().removeMapLayer(dialog.area_layer.id())

    btn_dibujar.clicked.connect(on_dibujar)
    btn_contorno_raster.clicked.connect(on_usar_contorno_raster)
    btn_borrar.clicked.connect(on_borrar_area)
    btn_procesar.clicked.connect(on_procesar)
    btn_exportar.clicked.connect(on_exportar)
    btn_acerca_de.clicked.connect(lambda: show_about_dialog(dialog))
    dialog.finished.connect(cleanup)
    dialog.exec()


# ===========================================================================
# CLASE PRINCIPAL DEL PLUGIN
# ===========================================================================

class GranulometriaPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = u'&Granulometría por Fotointerpretación'
        self.toolbar = self.iface.addToolBar(u'GranulometriaToolbar')
        self.toolbar.setObjectName(u'GranulometriaToolbar')

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, 'icon.png')
        self.action = QAction(QIcon(icon_path), u'Iniciar Análisis Granulométrico',
                              self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.toolbar.addAction(self.action)
        self.iface.addPluginToMenu(self.menu, self.action)
        self.actions.append(self.action)

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(u'&Granulometría por Fotointerpretación', action)
            self.iface.removeToolBarIcon(action)
        del self.toolbar

    def run(self):
        main_dialog(self.iface)
