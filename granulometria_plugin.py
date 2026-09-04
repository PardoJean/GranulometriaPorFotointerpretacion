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
import tempfile
import shutil

from qgis.core import (
    QgsField, QgsProject, QgsGeometry, QgsWkbTypes, QgsVectorLayer, QgsFeature,
    QgsSingleSymbolRenderer, QgsFillSymbol, QgsRasterLayer,
    QgsVectorFileWriter, QgsCoordinateTransform, Qgis, QgsApplication
)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
from qgis.PyQt.QtCore import QVariant, Qt, QSettings, QDate
from qgis.PyQt.QtWidgets import (
    QAction, QComboBox, QDialog, QVBoxLayout, QLabel, QPushButton, QMessageBox,
    QFileDialog, QFormLayout, QHBoxLayout, QProgressBar, QGroupBox,
    QGridLayout, QLineEdit, QCheckBox, QDialogButtonBox, QPlainTextEdit, QTabWidget,
    QWidget, QFrame, QMenu, QDateEdit, QSpinBox, QScrollArea
)
from qgis.PyQt.QtGui import (
    QIcon, QColor, QImage, QPainter, QFont, QFontMetrics, QPixmap, QTransform,
    QRegion, QBitmap
)

# ===========================================================================
# HOJA DE ESTILO ÚNICA (main_dialog, ExportDialog, show_about_dialog)
# ===========================================================================
# ponytail: sin colores/fuente propios — el diálogo hereda la paleta y fuente
# de QGIS (funciona igual en tema claro y oscuro). Solo se centra el título
# de los QGroupBox, que por defecto en Qt sale pegado a la izquierda.
DIALOG_STYLE = """
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top center; padding: 0 4px; }
"""

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
    """Herramienta de dibujo propia (QgsRubberBand), con los tres modos y
    nombres de QGIS pero sin tocar su maquinaria real de digitalización —
    QgsMapToolDigitizeFeature exige una capa en edición y crashea (access
    violation en QgsMapCanvas::setCurrentLayer) al desactivarse sobre una
    capa temporal que nunca se agregó al proyecto; esto no puede crashear
    porque no usa esa clase.

    modo: "segmento" (clic por vértice, clic derecho cierra),
          "flujo" (mantener presionado y mover, suelta para cerrar),
          "rectangulo" (dos clics, esquina y esquina opuesta).
    """
    def __init__(self, iface, on_polygon_drawn, on_cancel=None, modo="segmento"):
        super().__init__(iface.mapCanvas())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.on_polygon_drawn = on_polygon_drawn
        self.on_cancel = on_cancel
        self.modo = modo
        self.points = []
        self._streaming = False
        self.rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.rubber_band.setColor(QColor(255, 0, 0, 100))
        self.rubber_band.setWidth(2)

    def canvasPressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            if e.button() == Qt.MouseButton.RightButton:
                self.finalize_polygon()
            return
        pt = self.toMapCoordinates(e.pos())
        if self.modo == "flujo":
            self._streaming = True
            self.points = [pt]
            self._last_device_pos = e.pos()
        elif self.modo == "rectangulo":
            if len(self.points) < 1:
                self.points = [pt]
            else:
                self.points = self._rect_points(self.points[0], pt)
                self.finalize_polygon()
        else:  # segmento
            self.points.append(pt)
            self.update_rubber_band()

    def canvasMoveEvent(self, e):
        if self.modo == "flujo" and self._streaming:
            # Umbral de 4 px de pantalla entre vértices: evita miles de
            # puntos redundantes al arrastrar el mouse.
            last = getattr(self, "_last_device_pos", None)
            if last is not None:
                dx = e.pos().x() - last.x()
                dy = e.pos().y() - last.y()
                if dx * dx + dy * dy < 16:
                    return
            self._last_device_pos = e.pos()
            self.points.append(self.toMapCoordinates(e.pos()))
            self.update_rubber_band()
        elif self.modo == "rectangulo" and len(self.points) == 1:
            rect_pts = self._rect_points(self.points[0], self.toMapCoordinates(e.pos()))
            self.update_rubber_band(rect_pts=rect_pts)
        elif self.points:
            self.update_rubber_band(self.toMapCoordinates(e.pos()))

    def canvasReleaseEvent(self, e):
        if self.modo == "flujo" and self._streaming and e.button() == Qt.MouseButton.LeftButton:
            self._streaming = False
            self.finalize_polygon()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            if self.points:
                # Había un trazo en curso: solo se borra, la herramienta sigue activa.
                self.points = []
                self._streaming = False
                self.rubber_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
            else:
                # Nada que borrar: cancela y devuelve el control al diálogo.
                cb = self.on_cancel
                self.iface.mapCanvas().unsetMapTool(self)
                if cb:
                    cb()
        elif e.key() == Qt.Key.Key_Backspace and self.modo == "segmento" and self.points:
            self.points.pop()
            self.update_rubber_band()

    def _rect_points(self, p1, p2):
        from qgis.core import QgsPointXY
        return [QgsPointXY(p1.x(), p1.y()), QgsPointXY(p2.x(), p1.y()),
                QgsPointXY(p2.x(), p2.y()), QgsPointXY(p1.x(), p2.y())]

    def update_rubber_band(self, temporary_point=None, rect_pts=None):
        pts = rect_pts if rect_pts is not None else list(self.points)
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
                except Exception as e:
                    log(f"— No se pudieron generar las pirámides de la foto: {e}")
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
# RECORTE DE LA FOTOGRAFÍA (paso previo al análisis)
# ===========================================================================

def crop_raster_by_polygon(raster_layer, geom, out_path, log_fn=None):
    """Recorta la foto con un polígono de forma libre y escribe un GeoTIFF nuevo.

    Sin pérdida de calidad: se conserva la resolución original (xRes/yRes de la
    capa), el remuestreo es 'near' (ningún píxel se interpola) y la compresión
    es LZW (sin pérdida). Lo que queda fuera del polígono sale transparente
    gracias a la banda alfa, no en negro.

    Igual que en export_geopackage, no se reproyecta nada: el polígono se lleva
    al CRS del ráster si hiciera falta, pero la foto se escribe con las mismas
    coordenadas que ya tiene.
    """
    def log(msg):
        if log_fn:
            log_fn(msg)

    from osgeo import gdal
    gdal.UseExceptions()

    src_path = raster_layer.source().split('|')[0]
    tmp_dir = tempfile.mkdtemp(prefix="gfi_recorte_")
    cutline_path = os.path.join(tmp_dir, "recorte.gpkg")

    try:
        raster_crs = raster_layer.crs()
        proj_crs = QgsProject.instance().crs()
        crop_geom = QgsGeometry(geom)
        if proj_crs != raster_crs:
            tr = QgsCoordinateTransform(proj_crs, raster_crs, QgsProject.instance())
            crop_geom.transform(tr)

        cut_layer = QgsVectorLayer(f"Polygon?crs={raster_crs.authid()}", "recorte", "memory")
        feat = QgsFeature(cut_layer.fields())
        feat.setGeometry(crop_geom)
        cut_layer.dataProvider().addFeatures([feat])

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = "recorte"
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            cut_layer, cutline_path, QgsProject.instance().transformContext(), options
        )
        err_code = result[0] if isinstance(result, tuple) else result
        if err_code != QgsVectorFileWriter.WriterError.NoError:
            log(f"✘ No se pudo preparar el polígono de recorte: {result}")
            return False

        ds = gdal.Warp(
            out_path, src_path,
            cutlineDSName=cutline_path,
            cropToCutline=True,
            xRes=raster_layer.rasterUnitsPerPixelX(),
            yRes=raster_layer.rasterUnitsPerPixelY(),
            resampleAlg="near",
            dstAlpha=True,
            creationOptions=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"],
        )
        if ds is None:
            log("✘ GDAL no pudo generar el recorte de la foto.")
            return False
        ds = None
        log(f"✔ Foto recortada guardada en: {out_path}")
        return True
    except Exception as e:
        log(f"✘ Error al recortar la foto: {e}")
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def raster_footprint_geom(raster_layer, log_fn=None):
    """Polígono de los píxeles con datos del ráster (su huella real), no el
    rectángulo envolvente que da QgsRasterLayer.extent(). Las ortofotos de
    Metashape traen borde transparente/nodata fuera del área realmente
    fotografiada, así que el bbox sobreestima el área total (~13-39% medido
    contra fotos reales del laboratorio) y desalinea el % grueso/fino.

    Devuelve una QgsGeometry en el CRS del ráster, o None si no se pudo
    calcular (el llamador debe caer al extent() de siempre en ese caso).
    """
    def log(msg):
        if log_fn:
            log_fn(msg)

    try:
        from osgeo import gdal, ogr
        gdal.UseExceptions()

        src_path = raster_layer.source().split('|')[0]
        ds = gdal.Open(src_path)
        if ds is None:
            log("✘ No se pudo abrir el ráster con GDAL para calcular su huella.")
            return None

        band = ds.GetRasterBand(1)
        if band.GetMaskFlags() & gdal.GMF_ALL_VALID:
            # Sin alfa/nodata: no hay borde transparente, el bbox ya es la huella.
            return None

        mem = ogr.GetDriverByName("Memory").CreateDataSource("footprint")
        lyr = mem.CreateLayer("f", geom_type=ogr.wkbPolygon)
        lyr.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
        gdal.Polygonize(band.GetMaskBand(), band.GetMaskBand(), lyr, 0)

        union = None
        for feat in lyr:
            if feat.GetField("v") == 0:
                continue
            g = feat.GetGeometryRef()
            union = g.Clone() if union is None else union.Union(g)
        if union is None or union.GetArea() <= 0:
            log("✘ La huella calculada del ráster está vacía.")
            return None

        geom = QgsGeometry.fromWkt(union.ExportToWkt())
        if geom.constGet().nCoordinates() > 20000:
            px = raster_layer.rasterUnitsPerPixelX()
            geom = geom.simplify(px / 2)
            log(f"ℹ Huella simplificada por tener muchos vértices "
                f"(tolerancia {px / 2:.5f} m).")
        return geom
    except Exception as e:
        log(f"✘ Error al calcular la huella real del ráster: {e}")
        return None


# ===========================================================================
# ETIQUETADO DE FOTO (fecha + texto + logo, foto de evidencia para el informe)
# ===========================================================================

def preparar_foto_etiqueta(img):
    """Reemplaza por negro el relleno blanco puro de fondo (típico de las
    ortofotos de Metashape fuera del área realmente fotografiada — no es
    transparencia real: son píxeles (255,255,255) opacos ya horneados en la
    foto, confirmado contra fotos reales del laboratorio) y rota la imagen a
    horizontal si es vertical. La misma función se usa para la vista previa y
    para el archivo final exportado, para que ambos coincidan.

    Usa QImage.createMaskFromColor (nativo de Qt, en C++), no un bucle por
    píxel en Python — tarda <0.1 s incluso en fotos de 15+ MP. Es un
    reemplazo por color exacto: solo afecta relleno blanco puro y uniforme,
    no zonas claras con textura/variación natural de una foto real.
    """
    out = img.convertToFormat(QImage.Format.Format_RGB32)
    mask = out.createMaskFromColor(QColor(255, 255, 255).rgb(), Qt.MaskMode.MaskOutColor)
    region = QRegion(QBitmap.fromImage(mask))
    painter = QPainter(out)
    painter.setClipRegion(region)
    painter.fillRect(out.rect(), QColor("black"))
    painter.end()
    if out.height() > out.width():
        out = out.transformed(QTransform().rotate(90))
    return out


def compose_watermark(qimage, date_text, extra_text, logo_path, font_px, logo_width_px):
    """Devuelve una copia de qimage con el texto libre apilado arriba de la
    fecha, ambos abajo-izquierda, y el logo (si hay) solo abajo-derecha. No
    modifica qimage. Fuente Arial blanca, igual para fecha y texto.
    """
    out = QImage(qimage)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    margin = max(8, out.width() // 100)
    font = QFont("Arial")
    font.setPixelSize(max(1, font_px))
    painter.setFont(font)
    painter.setPen(QColor("white"))
    fm = QFontMetrics(font)

    y = out.height() - margin
    if date_text:
        painter.drawText(margin, y, date_text)
        y -= fm.height()
    if extra_text:
        painter.drawText(margin, y, extra_text)

    if logo_path and os.path.exists(logo_path) and logo_width_px > 0:
        logo = QImage(logo_path)
        if not logo.isNull():
            logo = logo.scaledToWidth(logo_width_px, Qt.TransformationMode.SmoothTransformation)
            x_logo = out.width() - margin - logo.width()
            y_logo = out.height() - margin - logo.height()
            painter.drawImage(x_logo, y_logo, logo)

    painter.end()
    return out


# ===========================================================================
# DIÁLOGOS
# ===========================================================================

def show_results_dialog(resumen, params, umbral_pulgadas):
    """Diálogo de resultados: áreas total/grueso/fino + parámetros geotécnicos."""
    dlg = QDialog()
    dlg.setWindowTitle("Resultados del Análisis Granulométrico")
    dlg.setMinimumWidth(440)
    dlg.setStyleSheet(DIALOG_STYLE)

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
    grp0 = QGroupBox("Áreas")
    g0 = QGridLayout(grp0)
    g0.setVerticalSpacing(4)
    area_data = [
        ("Área total:", f"{resumen['area_total']:.4f} m²"),
        ("Área material grueso:", f"{resumen['area_gruesos']:.4f} m²  ({resumen['pct_gruesos']:.2f} %)"),
        ("Área material fino:", f"{resumen['area_finos']:.4f} m²  ({resumen['pct_finos']:.2f} %)"),
    ]
    for i, (k, v) in enumerate(area_data):
        lbl_k = QLabel(k); lbl_k.setStyleSheet("font-weight: bold;")
        lbl_v = QLabel(v); lbl_v.setStyleSheet("font-weight: bold;")
        lbl_v.setAlignment(Qt.AlignmentFlag.AlignRight)
        g0.addWidget(lbl_k, i, 0)
        g0.addWidget(lbl_v, i, 1)
    layout.addWidget(grp0)

    # --- Resumen procesamiento ---
    grp1 = QGroupBox("Resumen de Procesamiento")
    g1 = QGridLayout(grp1)
    g1.setVerticalSpacing(4)
    proc_data = [
        ("Polígonos iniciales:", str(resumen['iniciales'])),
        (f"Eliminados (< {umbral_pulgadas}\"):", str(resumen['eliminados'])),
        ("Polígonos finales:", str(resumen['finales'])),
        ("Polígonos irregulares:", str(resumen['irregulares'])),
    ]
    for i, (k, v) in enumerate(proc_data):
        lbl_k = QLabel(k); lbl_k.setStyleSheet("font-weight: bold;")
        lbl_v = QLabel(v); lbl_v.setStyleSheet("font-weight: bold;")
        lbl_v.setAlignment(Qt.AlignmentFlag.AlignRight)
        g1.addWidget(lbl_k, i, 0)
        g1.addWidget(lbl_v, i, 1)
    layout.addWidget(grp1)

    # --- Parámetros geotécnicos (solo en pantalla) ---
    grp2 = QGroupBox("Parámetros Granulométricos")
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
        lk = QLabel(k); lk.setStyleSheet("font-weight: bold;")
        lv = QLabel(v)
        # Color semántico de la clasificación SUCS (bien/mal graduado), se conserva.
        if "GW" in v:
            lv.setStyleSheet("color: #27ae60; font-weight: bold;")
        elif "GP" in v:
            lv.setStyleSheet("color: #e74c3c; font-weight: bold;")
        else:
            lv.setStyleSheet("font-weight: bold;")
        lv.setAlignment(Qt.AlignmentFlag.AlignRight)
        g2.addWidget(lk, i, 0)
        g2.addWidget(lv, i, 1)
    layout.addWidget(grp2)

    btn = QPushButton("Aceptar")
    btn.setObjectName("primary_button")
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


PLUGIN_VERSION = "1.0.2"
PLUGIN_FECHA = "2026-09-04"

ACERCA_DE_QUE_HACE = (
    "Este plugin mide el tamaño de los granos de roca en una foto y calcula qué "
    "porcentaje del área es material grueso y qué porcentaje es material fino.\n\n"
    "0) (Opcional) En la pestaña «Preparar Foto» recortas la fotografía con un "
    "polígono de forma libre, para quedarte solo con la zona representativa. El "
    "recorte se guarda como un GeoTIFF nuevo, sin pérdida de calidad, y se carga "
    "listo para analizarlo.\n"
    "1) Dibujas o generas el contorno de toda la foto: esa es el área total.\n"
    "2) El plugin mide cada polígono (grano) que hayas segmentado y descarta los "
    "más pequeños que el tamiz que elijas.\n"
    "3) El área de los granos que quedan (≥ tamiz) es el material grueso.\n"
    "4) El área total menos el área de gruesos es el material fino."
)

ACERCA_DE_LICENCIA = (
    "Licencia GNU GPLv3: puedes copiar, redistribuir y modificar este plugin "
    "libremente, incluso con fines comerciales. Si vas a adaptarlo o "
    "integrarlo en un proyecto propio, se agradece que primero te pongas en "
    "contacto con el autor.\n\n"
    "Jean Pardo — jeandariopardo@gmail.com"
)

ACERCA_DE_NOVEDADES = (
    "Versión 1.0.2:\n"
    "  • Nuevo paso opcional «Preparar Foto»: recortar la fotografía con un "
    "polígono de forma libre (sin pérdida de calidad) antes de analizarla, con "
    "tres formas de dibujar el contorno: «Por segmento», «Por flujo» y «Por "
    "forma: Rectángulo».\n"
    "  • El área total ahora usa la huella real de píxeles de la foto (no el "
    "rectángulo envolvente), que en fotos con borde transparente sobreestimaba "
    "el área hasta en un 39 %.\n"
    "  • El contorno de área total se puede dibujar con los mismos tres modos "
    "que el recorte, y el grupo «Área Total» ahora muestra y permite elegir "
    "sobre qué foto se está trabajando.\n"
    "  • Interfaz reorganizada en pestañas («Preparar Foto», «Analizar», "
    "«Etiquetar Foto»), con apariencia nativa de QGIS (tema y fuente heredados, "
    "íconos del propio QGIS en vez de emojis) y foco de teclado visible.\n"
    "  • Nueva pestaña «Etiquetar Foto»: pega sobre la fotografía la fecha "
    "(calendario, formato AAAA-MM-DD, abajo-izquierda), un texto libre y un "
    "logo propio (recordado de forma privada) abajo-derecha, y la exporta como "
    "imagen aparte para el informe.\n"
    "  • «Procesar Capa» ya no abre la tabla de atributos ni deja la capa "
    "temporal del área total en el panel tras exportar.\n\n"
    "Versión 1.0.1:\n"
    "  • Compatibilidad con Qt6 (enums calificados, imports vía qgis.PyQt).\n\n"
    "Versión 1.0.0:\n"
    "  • Primera versión pública del plugin."
)


def show_about_dialog(parent=None):
    dlg = QDialog(parent)
    dlg.setWindowTitle("Acerca de — Granulometría por Fotointerpretación")
    dlg.setMinimumWidth(480)
    dlg.setStyleSheet(DIALOG_STYLE)
    layout = QVBoxLayout(dlg)
    layout.setSpacing(10)
    layout.setContentsMargins(14, 14, 14, 14)

    title = QLabel("Análisis Granulométrico por Fotointerpretación")
    title.setStyleSheet("font-size: 12pt; font-weight: bold;")
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

    grp3 = QGroupBox("Licencia y contacto")
    l3 = QVBoxLayout(grp3)
    lbl3 = QLabel(ACERCA_DE_LICENCIA)
    lbl3.setWordWrap(True)
    lbl3.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    l3.addWidget(lbl3)
    layout.addWidget(grp3)

    lbl_meta = QLabel(f"Versión {PLUGIN_VERSION}  ·  {PLUGIN_FECHA}")
    lbl_meta.setStyleSheet("font-size: 9pt;")
    lbl_meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(lbl_meta)

    btn = QPushButton("Cerrar")
    btn.setObjectName("primary_button")
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
        self.setStyleSheet(DIALOG_STYLE)

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
        lbl_crs_info.setStyleSheet("font-size: 9pt;")
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

    dialog.setStyleSheet(DIALOG_STYLE)

    # Estado del diálogo para compartir entre funciones
    dialog._resumen = None
    dialog._gradation = None
    dialog._params = None
    dialog._crop_geom = None

    def _crear_area_layer():
        crs = QgsProject.instance().crs().authid()
        lyr = QgsVectorLayer(f"Polygon?crs={crs}", "Área Total (Temporal)", "memory")
        QgsProject.instance().addMapLayer(lyr)
        symbol = QgsFillSymbol.createSimple(
            {'color': '0,115,230,40', 'outline_color': '#0073e6', 'outline_width': '0.6'}
        )
        lyr.setRenderer(QgsSingleSymbolRenderer(symbol))
        return lyr

    dialog.area_layer = _crear_area_layer()
    dialog.map_tool = None

    # Contorno del recorte, visible sobre el lienzo mientras el diálogo vive
    dialog._crop_rubber = QgsRubberBand(iface.mapCanvas(),
                                        QgsWkbTypes.GeometryType.PolygonGeometry)
    dialog._crop_rubber.setColor(QColor(0, 153, 76, 60))
    dialog._crop_rubber.setStrokeColor(QColor(0, 153, 76, 255))
    dialog._crop_rubber.setWidth(2)

    layout = QVBoxLayout(dialog)
    layout.setSpacing(12)
    layout.setContentsMargins(15, 15, 15, 15)

    # --- Título ---
    title = QLabel("Análisis Granulométrico por Fotointerpretación")
    title.setStyleSheet("font-size: 14pt; font-weight: bold; margin-bottom: 4px;")
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(title)

    # Selector de la foto: vive en la pestaña "Preparar Foto", pero es el mismo
    # combo que usa "Usar Contorno del Ráster" en la pestaña "Analizar".
    combo_raster = QComboBox()
    for r in get_raster_layers():
        combo_raster.addItem(r.name(), r.id())

    # --- Grupo 1: Capas de trabajo ---
    grp_capas = QGroupBox("1. Capas de Trabajo")
    lay_capas = QFormLayout(grp_capas)
    lay_capas.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
    combo_layer = QComboBox()
    for lyr in layers:
        combo_layer.addItem(lyr.name(), lyr.id())
    lay_capas.addRow("Capa de polígonos:", combo_layer)
    lbl_count = QLabel("—")
    lbl_count.setStyleSheet("font-weight: bold;")
    lay_capas.addRow("Polígonos en capa:", lbl_count)

    # --- Grupo 2: Área total (contorno de la foto) ---
    grp_area = QGroupBox("2. Área Total (Contorno de la Foto)")
    lay_area = QVBoxLayout(grp_area)
    lay_area.setSpacing(8)
    # Espejo del selector de foto (combo_raster, en "Preparar Foto"): sin esto
    # no se ve desde esta pestaña sobre qué foto van a actuar estos botones.
    form_foto_area = QFormLayout()
    combo_raster_area = QComboBox()
    for r in get_raster_layers():
        combo_raster_area.addItem(r.name(), r.id())
    form_foto_area.addRow("Fotografía:", combo_raster_area)
    lay_area.addLayout(form_foto_area)
    hbtn = QHBoxLayout()
    btn_contorno_raster = QPushButton(QgsApplication.getThemeIcon("mActionAddRasterLayer.svg"),
                                      "Usar Contorno del Ráster")
    # Menú con los mismos tres modos de dibujo que "Dibujar Recorte" — nombres
    # e íconos de QGIS (por segmento / por flujo / por forma: rectángulo).
    btn_dibujar = QPushButton("Dibujar Contorno")
    menu_dibujar = QMenu(btn_dibujar)
    act_area_segmento = menu_dibujar.addAction(
        QgsApplication.getThemeIcon("mActionDigitizeWithSegment.svg"), "Por segmento")
    act_area_flujo = menu_dibujar.addAction(
        QgsApplication.getThemeIcon("mActionCapturePolygon.svg"), "Por flujo")
    act_area_forma = menu_dibujar.addAction(
        QgsApplication.getThemeIcon("mActionSelectRectangle.svg"), "Por forma: Rectángulo")
    btn_dibujar.setMenu(menu_dibujar)
    btn_borrar = QPushButton(QgsApplication.getThemeIcon("mActionDeleteSelected.svg"), "Borrar")
    hbtn.addWidget(btn_contorno_raster)
    hbtn.addWidget(btn_dibujar)
    hbtn.addWidget(btn_borrar)
    lbl_area_total = QLabel("Área total: — (genera o dibuja el contorno)")
    lbl_area_total.setStyleSheet("font-weight: bold;")
    lay_area.addLayout(hbtn)
    lay_area.addWidget(lbl_area_total)

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

    # --- Grupo: Recorte de la foto ---
    grp_recorte = QGroupBox("Recorte (opcional)")
    lay_recorte = QVBoxLayout(grp_recorte)
    lay_recorte.setSpacing(8)
    hbtn_recorte = QHBoxLayout()
    # Menú con los nombres de QGIS para los tres modos (por segmento / por
    # flujo / por forma: rectángulo), dibujados con PolygonMapTool propio —
    # ver la nota en esa clase sobre por qué no usa QgsMapToolDigitizeFeature.
    btn_dibujar_recorte = QPushButton("Dibujar Recorte")
    menu_recorte = QMenu(btn_dibujar_recorte)
    act_recorte_segmento = menu_recorte.addAction(
        QgsApplication.getThemeIcon("mActionDigitizeWithSegment.svg"), "Por segmento")
    act_recorte_flujo = menu_recorte.addAction(
        QgsApplication.getThemeIcon("mActionCapturePolygon.svg"), "Por flujo")
    act_recorte_forma = menu_recorte.addAction(
        QgsApplication.getThemeIcon("mActionSelectRectangle.svg"), "Por forma: Rectángulo")
    btn_dibujar_recorte.setMenu(menu_recorte)
    btn_guardar_recorte = QPushButton(QgsApplication.getThemeIcon("mActionFileSaveAs.svg"),
                                      "Guardar y Exportar…")
    btn_guardar_recorte.setEnabled(False)
    hbtn_recorte.addWidget(btn_dibujar_recorte)
    hbtn_recorte.addWidget(btn_guardar_recorte)
    lbl_recorte_estado = QLabel("Sin recorte definido.")
    lay_recorte.addLayout(hbtn_recorte)
    lay_recorte.addWidget(lbl_recorte_estado)

    # --- Grupo: Datos de la imagen ---
    grp_info = QGroupBox("Datos de la Imagen")
    lay_info = QFormLayout(grp_info)
    lay_info.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
    lbl_dims = QLabel("—")
    lbl_mp = QLabel("—")
    lbl_mp.setStyleSheet("font-weight: bold;")
    lbl_pixel = QLabel("—")
    lay_info.addRow("Dimensiones (px):", lbl_dims)
    lay_info.addRow("Tamaño de imagen:", lbl_mp)
    lay_info.addRow("Tamaño de píxel (m):", lbl_pixel)

    # --- Grupo: Etiquetar foto (fecha + texto + logo, para el informe) ---
    grp_etiqueta = QGroupBox("Etiquetar Foto")
    lay_etiqueta = QVBoxLayout(grp_etiqueta)
    lay_etiqueta.setSpacing(8)

    form_etiqueta = QFormLayout()
    form_etiqueta.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
    combo_raster_etiqueta = QComboBox()
    for r in get_raster_layers():
        combo_raster_etiqueta.addItem(r.name(), r.id())
    form_etiqueta.addRow("Fotografía:", combo_raster_etiqueta)

    txt_extra = QLineEdit()
    txt_extra.setPlaceholderText("Texto libre (abajo-izq., sobre la fecha), p. ej. sitio o técnico")
    form_etiqueta.addRow("Texto (abajo-izq.):", txt_extra)

    date_edit = QDateEdit()
    date_edit.setCalendarPopup(True)
    date_edit.setDisplayFormat("yyyy-MM-dd")
    date_edit.setDate(QDate.currentDate())
    form_etiqueta.addRow("Fecha (abajo-izq., bajo el texto):", date_edit)
    lay_etiqueta.addLayout(form_etiqueta)

    h_logo = QHBoxLayout()
    btn_logo = QPushButton(QgsApplication.getThemeIcon("mActionAddRasterLayer.svg"), "Cargar Logo…")
    lbl_logo_preview = QLabel("Sin logo")
    lbl_logo_preview.setFixedSize(48, 48)
    lbl_logo_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl_logo_preview.setStyleSheet("border: 1px solid #999999;")
    h_logo.addWidget(btn_logo)
    h_logo.addWidget(lbl_logo_preview)
    h_logo.addStretch()
    lay_etiqueta.addLayout(h_logo)

    h_tam = QHBoxLayout()
    spin_font = QSpinBox()
    spin_font.setRange(10, 80)
    spin_font.setSuffix(" px")
    spin_logo = QSpinBox()
    spin_logo.setRange(20, 800)
    spin_logo.setSuffix(" px")
    h_tam.addWidget(QLabel("Tamaño de texto:"))
    h_tam.addWidget(spin_font)
    h_tam.addSpacing(12)
    h_tam.addWidget(QLabel("Ancho del logo:"))
    h_tam.addWidget(spin_logo)
    h_tam.addStretch()
    lay_etiqueta.addLayout(h_tam)

    lbl_preview_etiqueta = QLabel("Selecciona una fotografía para ver la vista previa.")
    lbl_preview_etiqueta.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl_preview_etiqueta.setMinimumHeight(260)
    lbl_preview_etiqueta.setStyleSheet("border: 1px solid #999999;")
    lay_etiqueta.addWidget(lbl_preview_etiqueta)

    btn_guardar_etiqueta = QPushButton(QgsApplication.getThemeIcon("mActionFileSaveAs.svg"),
                                       "Guardar Imagen…")
    lay_etiqueta.addWidget(btn_guardar_etiqueta)

    # --- Pestañas: primero se prepara la foto, después se analiza ---
    tab_preparar = QWidget()
    lay_preparar = QVBoxLayout(tab_preparar)
    lay_preparar.setSpacing(12)
    form_foto = QFormLayout()
    form_foto.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
    form_foto.addRow("Fotografía (ráster):", combo_raster)
    lay_preparar.addLayout(form_foto)
    lay_preparar.addWidget(grp_recorte)
    lay_preparar.addWidget(grp_info)
    lay_preparar.addStretch()

    tab_analizar = QWidget()
    lay_analizar = QVBoxLayout(tab_analizar)
    lay_analizar.setSpacing(12)
    lay_analizar.addWidget(grp_capas)
    lay_analizar.addWidget(grp_area)
    lay_analizar.addWidget(grp_params)
    lay_analizar.addStretch()

    # Envuelto en QScrollArea: sin esto, el sizeHint() de esta pestaña (la más
    # alta, por la vista previa) se propaga al QTabWidget entero y estira
    # "Preparar Foto"/"Analizar" aunque no lo necesiten — QScrollArea acota su
    # propio sizeHint en vez de heredar el alto real del contenido.
    scroll_etiqueta = QScrollArea()
    scroll_etiqueta.setWidgetResizable(True)
    scroll_etiqueta.setFrameShape(QFrame.Shape.NoFrame)
    scroll_etiqueta.setWidget(grp_etiqueta)

    tab_etiqueta = QWidget()
    lay_tab_etiqueta = QVBoxLayout(tab_etiqueta)
    lay_tab_etiqueta.setContentsMargins(0, 0, 0, 0)
    lay_tab_etiqueta.addWidget(scroll_etiqueta)

    tabs = QTabWidget()
    tabs.addTab(tab_preparar, "Preparar Foto")
    tabs.addTab(tab_analizar, "Analizar")
    tabs.addTab(tab_etiqueta, "Etiquetar Foto")
    layout.addWidget(tabs)

    # --- Barra de progreso ---
    progress_bar = QProgressBar()
    progress_bar.setVisible(False)
    layout.addWidget(progress_bar)

    # --- Botones de acción (fuera de las pestañas: siempre visibles) ---
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    layout.addWidget(sep)

    hact = QHBoxLayout()
    btn_acerca_de = QPushButton(QgsApplication.getThemeIcon("mActionHelpContents.svg"),
                                "Acerca de")
    hact.addWidget(btn_acerca_de)
    hact.addStretch()
    btn_procesar = QPushButton(QgsApplication.getThemeIcon("mActionStart.svg"), "Procesar Capa")
    btn_procesar.setObjectName("primary_button")
    btn_exportar = QPushButton(QgsApplication.getThemeIcon("mActionFileSave.svg"), "Exportar Todo")
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
    # Sincronización bidireccional con el combo espejo del grupo 2: al ser el
    # mismo índice, Qt no vuelve a emitir la señal, así que no hace falta
    # blockSignals ni riesgo de bucle infinito.
    combo_raster.currentIndexChanged.connect(combo_raster_area.setCurrentIndex)
    combo_raster_area.currentIndexChanged.connect(combo_raster.setCurrentIndex)
    combo_raster.currentIndexChanged.connect(combo_raster_etiqueta.setCurrentIndex)
    combo_raster_etiqueta.currentIndexChanged.connect(combo_raster.setCurrentIndex)
    update_image_info()

    # ---- Parsear umbral ----
    def get_umbral():
        raw = combo_umbral.currentText().replace('"', '').strip()
        return parse_inches(raw)

    # ---- Área total: helpers ----
    def set_area_geom(geom):
        if dialog.area_layer is None:
            dialog.area_layer = _crear_area_layer()
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

    _MODO_MSG = {
        "segmento": "Por segmento: clic izquierdo para cada vértice, clic derecho para cerrar.",
        "flujo": "Por flujo: mantén presionado el clic y mueve el mouse; suelta para cerrar.",
        "rectangulo": "Por forma (rectángulo): clic en una esquina, clic en la opuesta.",
    }

    def _start_dibujo(modo, on_geom, on_cancel, extra_msg=""):
        dialog.hide()
        dialog.map_tool = PolygonMapTool(iface, on_geom, on_cancel, modo)
        iface.mapCanvas().setMapTool(dialog.map_tool)
        iface.messageBar().pushMessage("Herramienta activada",
                                       _MODO_MSG[modo] + extra_msg, duration=7)

    def on_dibujar(modo):
        _start_dibujo(modo, on_drawing_complete, lambda: dialog.show(),
                     " Escape borra el trazo (o cancela si ya no había nada).")

    # ---- Recorte de la foto ----
    def get_selected_raster():
        rid = combo_raster.currentData()
        if not rid:
            QMessageBox.warning(dialog, "Advertencia", "Selecciona primero una foto (ráster).")
            return None
        rlyr = QgsProject.instance().mapLayer(rid)
        if not rlyr:
            QMessageBox.critical(dialog, "Error", "No se pudo encontrar la capa ráster seleccionada.")
            return None
        return rlyr

    def on_recorte_dibujado(geom):
        dialog.show()
        dialog._crop_geom = geom
        dialog._crop_rubber.setToGeometry(geom, None)
        iface.mapCanvas().refresh()
        lbl_recorte_estado.setText(f"Recorte definido: {geom.area():.4f} m²")
        btn_guardar_recorte.setEnabled(True)

    def on_dibujar_recorte(modo):
        """Limpia el recorte anterior (geom + rubber band verde) y arranca el
        dibujo en el modo elegido (segmento/flujo/rectángulo)."""
        if get_selected_raster() is None:
            return
        dialog._crop_geom = None
        dialog._crop_rubber.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        lbl_recorte_estado.setText("Sin recorte definido.")
        btn_guardar_recorte.setEnabled(False)
        iface.mapCanvas().refresh()
        _start_dibujo(modo, on_recorte_dibujado, lambda: dialog.show(),
                     " Escape borra el trazo (o cancela si ya no había nada).")

    def on_guardar_recorte():
        rlyr = get_selected_raster()
        if rlyr is None or dialog._crop_geom is None:
            return

        settings = QSettings()
        last_folder = settings.value("GranulometriaGFI/ultima_carpeta", "")
        default_name = f"{rlyr.name()}_recorte.tif"
        default_path = os.path.join(last_folder, default_name) if last_folder else default_name
        out_path, _ = QFileDialog.getSaveFileName(
            dialog, "Guardar foto recortada", default_path, "GeoTIFF (*.tif)"
        )
        if not out_path:
            return
        if not out_path.lower().endswith(".tif"):
            out_path += ".tif"

        mensajes = []
        ok = crop_raster_by_polygon(rlyr, dialog._crop_geom, out_path, log_fn=mensajes.append)
        if not ok:
            QMessageBox.critical(dialog, "Error",
                                 "No se pudo recortar la foto:\n\n" + "\n".join(mensajes))
            return

        settings.setValue("GranulometriaGFI/ultima_carpeta", os.path.dirname(out_path))

        nueva = QgsRasterLayer(out_path, os.path.splitext(os.path.basename(out_path))[0])
        if nueva.isValid():
            QgsProject.instance().addMapLayer(nueva)
            combo_raster.addItem(nueva.name(), nueva.id())
            combo_raster_area.addItem(nueva.name(), nueva.id())
            combo_raster_etiqueta.addItem(nueva.name(), nueva.id())
            combo_raster.setCurrentIndex(combo_raster.count() - 1)
            iface.messageBar().pushMessage("Éxito",
                                           "Foto recortada guardada y cargada en el proyecto.",
                                           level=Qgis.MessageLevel.Success, duration=5)
        else:
            QMessageBox.warning(dialog, "Aviso",
                                "El recorte se guardó, pero no se pudo cargar automáticamente:\n"
                                + out_path)

    # ---- Etiquetar foto (fecha + texto + logo, para el informe) ----
    def _raster_etiqueta():
        rid = combo_raster_etiqueta.currentData()
        return QgsProject.instance().mapLayer(rid) if rid else None

    def _logo_path():
        return QSettings().value("GranulometriaGFI/logo_path", "")

    def _mostrar_logo_preview(path):
        if path and os.path.exists(path):
            pix = QPixmap(path)
            if not pix.isNull():
                lbl_logo_preview.setPixmap(
                    pix.scaled(46, 46, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation))
                return
        lbl_logo_preview.setPixmap(QPixmap())
        lbl_logo_preview.setText("Sin logo")

    def on_cargar_logo():
        settings = QSettings()
        last = settings.value("GranulometriaGFI/logo_path", "")
        path, _ = QFileDialog.getOpenFileName(
            dialog, "Cargar logo", os.path.dirname(last) if last else "",
            "Imágenes (*.png *.jpg *.jpeg)")
        if not path:
            return
        settings.setValue("GranulometriaGFI/logo_path", path)
        _mostrar_logo_preview(path)
        update_preview_etiqueta()

    def _ajustar_tamanos_por_defecto():
        spin_font.blockSignals(True)
        spin_logo.blockSignals(True)
        spin_font.setValue(60)
        spin_logo.setValue(800)
        spin_font.blockSignals(False)
        spin_logo.blockSignals(False)

    def update_preview_etiqueta():
        rlyr = _raster_etiqueta()
        if rlyr is None:
            lbl_preview_etiqueta.setPixmap(QPixmap())
            lbl_preview_etiqueta.setText("Selecciona una fotografía para ver la vista previa.")
            return
        img = QImage(rlyr.source().split('|')[0])
        if img.isNull():
            lbl_preview_etiqueta.setPixmap(QPixmap())
            lbl_preview_etiqueta.setText("No se pudo leer la fotografía.")
            return
        # Misma preparación (fondo negro + rotación) que on_guardar_etiqueta,
        # para que la vista previa coincida exactamente con lo que se exporta.
        img_previa = preparar_foto_etiqueta(img)

        box_w, box_h = 480, 260
        if img_previa.width() > box_w or img_previa.height() > box_h:
            small = img_previa.scaled(box_w, box_h, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
        else:
            small = img_previa
        # Los tamaños de fuente/logo están en píxeles de la imagen real; se
        # escalan por el mismo factor que la miniatura para que la vista
        # previa coincida visualmente con lo que sale al exportar en tamaño
        # completo.
        factor = small.width() / img_previa.width()
        composed = compose_watermark(
            small, date_edit.date().toString("yyyy-MM-dd"), txt_extra.text(),
            _logo_path(), max(1, round(spin_font.value() * factor)),
            max(1, round(spin_logo.value() * factor)))
        lbl_preview_etiqueta.setText("")
        lbl_preview_etiqueta.setPixmap(QPixmap.fromImage(composed))

    def on_raster_etiqueta_changed():
        _ajustar_tamanos_por_defecto()
        update_preview_etiqueta()

    def on_guardar_etiqueta():
        rlyr = _raster_etiqueta()
        if rlyr is None:
            QMessageBox.warning(dialog, "Advertencia", "Selecciona primero una fotografía.")
            return
        img = QImage(rlyr.source().split('|')[0])
        if img.isNull():
            QMessageBox.critical(dialog, "Error", "No se pudo leer la fotografía seleccionada.")
            return
        img_final = preparar_foto_etiqueta(img)

        settings = QSettings()
        last_folder = settings.value("GranulometriaGFI/ultima_carpeta", "")
        # Nombre sugerido = la misma fecha que se pega en la foto; el usuario
        # puede agregarle algo al final en el propio diálogo de guardado para
        # distinguir varias del mismo día.
        default_name = f"{date_edit.date().toString('yyyy-MM-dd')}.png"
        default_path = os.path.join(last_folder, default_name) if last_folder else default_name
        out_path, _ = QFileDialog.getSaveFileName(
            dialog, "Guardar foto etiquetada", default_path, "PNG (*.png);;JPEG (*.jpg)"
        )
        if not out_path:
            return

        resultado = compose_watermark(
            img_final, date_edit.date().toString("yyyy-MM-dd"), txt_extra.text(),
            _logo_path(), spin_font.value(), spin_logo.value())
        if not resultado.save(out_path):
            QMessageBox.critical(dialog, "Error", "No se pudo guardar la imagen.")
            return

        settings.setValue("GranulometriaGFI/ultima_carpeta", os.path.dirname(out_path))
        iface.messageBar().pushMessage("Éxito", "Foto etiquetada guardada.",
                                       level=Qgis.MessageLevel.Success, duration=5)

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

        # Huella real de los píxeles con datos (no el rectángulo envolvente):
        # las ortofotos traen borde transparente y el bbox sobreestima el área.
        footprint_log = []
        geom = raster_footprint_geom(rlyr, log_fn=footprint_log.append)
        es_huella = geom is not None
        if not es_huella:
            geom = QgsGeometry.fromRect(rlyr.extent())

        if rlyr.crs() != proj_crs:
            tr = QgsCoordinateTransform(rlyr.crs(), proj_crs, QgsProject.instance())
            try:
                geom.transform(tr)
            except Exception:
                QMessageBox.critical(dialog, "Error",
                                     "No se pudo reproyectar el contorno del ráster al CRS del proyecto.")
                return

        if proj_crs.isGeographic():
            QMessageBox.warning(dialog, "Aviso",
                                "El proyecto está en un CRS geográfico (grados). "
                                "Las áreas calculadas no estarán en m² reales; "
                                "usa un CRS proyectado (p. ej. UTM) para resultados correctos.")

        set_area_geom(geom)
        origen = "huella real de la foto" if es_huella else "rectángulo envolvente del ráster"
        lbl_area_total.setText(lbl_area_total.text() + f"  ({origen})")
        iface.mapCanvas().refresh()

    def on_borrar_area():
        if dialog.area_layer is None:
            return
        dialog.area_layer.dataProvider().truncate()
        dialog.area_layer.updateExtents()
        iface.mapCanvas().refresh()
        lbl_area_total.setText("Área total: — (genera o dibuja el contorno)")

    def get_area_total_geom():
        if dialog.area_layer is None:
            return None
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
            # La capa "Área Total (Temporal)" ya cumplió su función (el área
            # quedó grabada en los reportes); se quita del panel de capas. Si
            # el usuario dibuja/genera un contorno de nuevo, se recrea sola
            # (ver set_area_geom).
            if dialog.area_layer:
                QgsProject.instance().removeMapLayer(dialog.area_layer.id())
                dialog.area_layer = None
                iface.mapCanvas().refresh()
                lbl_area_total.setText("Área total: — (genera o dibuja el contorno)")

            resumen_gpkg = ("\n\n" + "\n".join(gpkg_log)) if gpkg_log else ""
            QMessageBox.information(dialog, "Exportación completa",
                                    "✅ Archivos guardados en:\n" + folder + "\n\n" +
                                    "\n".join(f"  • {os.path.basename(p)}" for p in generated) +
                                    resumen_gpkg)
        else:
            QMessageBox.warning(dialog, "Advertencia", "No se generó ningún archivo.")

    # ---- Procesar/Exportar solo tienen sentido en la pestaña "Analizar" ----
    def on_tab_changed(index):
        en_analizar = (tabs.widget(index) is tab_analizar)
        btn_procesar.setVisible(en_analizar)
        btn_exportar.setVisible(en_analizar)

    def cleanup():
        if dialog.area_layer:
            QgsProject.instance().removeMapLayer(dialog.area_layer.id())
        if dialog._crop_rubber:
            dialog._crop_rubber.reset(QgsWkbTypes.GeometryType.PolygonGeometry)

    act_recorte_segmento.triggered.connect(lambda: on_dibujar_recorte("segmento"))
    act_recorte_flujo.triggered.connect(lambda: on_dibujar_recorte("flujo"))
    act_recorte_forma.triggered.connect(lambda: on_dibujar_recorte("rectangulo"))
    act_area_segmento.triggered.connect(lambda: on_dibujar("segmento"))
    act_area_flujo.triggered.connect(lambda: on_dibujar("flujo"))
    act_area_forma.triggered.connect(lambda: on_dibujar("rectangulo"))
    btn_guardar_recorte.clicked.connect(on_guardar_recorte)
    btn_contorno_raster.clicked.connect(on_usar_contorno_raster)
    btn_borrar.clicked.connect(on_borrar_area)
    btn_procesar.clicked.connect(on_procesar)
    btn_exportar.clicked.connect(on_exportar)
    btn_acerca_de.clicked.connect(lambda: show_about_dialog(dialog))
    tabs.currentChanged.connect(on_tab_changed)
    on_tab_changed(tabs.currentIndex())

    btn_logo.clicked.connect(on_cargar_logo)
    btn_guardar_etiqueta.clicked.connect(on_guardar_etiqueta)
    combo_raster_etiqueta.currentIndexChanged.connect(on_raster_etiqueta_changed)
    date_edit.dateChanged.connect(update_preview_etiqueta)
    txt_extra.textChanged.connect(update_preview_etiqueta)
    spin_font.valueChanged.connect(update_preview_etiqueta)
    spin_logo.valueChanged.connect(update_preview_etiqueta)
    _mostrar_logo_preview(_logo_path())
    _ajustar_tamanos_por_defecto()
    update_preview_etiqueta()

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
