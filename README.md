# Granulometria por Fotointerpretacion

A QGIS plugin for photointerpretation-based grain size analysis. It measures
rock grain sizes in a georeferenced photo (typically captured by drone),
computes the coarse/fine material split by area, and exports the results as
ready-to-use reports.

## What it does

1. **Total area** — draw the outline of the photo (or generate it
   automatically from the raster extent) to get the total surveyed area.
2. **Grain measurement** — the plugin measures every polygon (grain) drawn
   or segmented on top of the photo. Polygons smaller than a chosen sieve
   size are discarded from the "coarse" count.
3. **Coarse material** — the summed area of the grains that meet or exceed
   the sieve threshold.
4. **Fine material** — obtained by difference: total area minus coarse area.

Grain polygons are typically produced with a segmentation tool (for example,
a Segment Anything Model-based QGIS plugin) and then manually adjusted
before running the analysis — this plugin does not perform segmentation
itself, only measurement and reporting.

## Requirements

- QGIS 3.0 or newer (tested on QGIS 3.42).
- No external Python packages. The plugin only uses `PyQt5` and
  `qgis.core` / `qgis.gui`, which ship with every standard QGIS
  installation (Windows/OSGeo4W, Linux, macOS) — nothing extra to install.

## Installation

**From the QGIS Plugin Repository (recommended):**

1. In QGIS, open *Plugins → Manage and Install Plugins*.
2. Search for "Granulometria por Fotointerpretacion" and click *Install*.

**From a ZIP file:**

1. Download or build the plugin ZIP (the top-level folder inside must be
   named `GranulometriaPorFotointerpretacion`, containing `metadata.txt`,
   `__init__.py`, `granulometria_plugin.py`, `icon.png` and `LICENSE`).
2. In QGIS, open *Plugins → Manage and Install Plugins → Install from ZIP*.
3. Select the ZIP file and click *Install Plugin*.

## Basic usage

1. Load your georeferenced photo as a raster layer and your grain polygons
   as a vector layer in the same QGIS project.
2. Open the plugin panel (toolbar icon or *Plugins* menu).
3. Under **1. Working layers**, select the polygon layer and the photo
   raster.
4. Under **2. Total area**, either use the raster outline or draw the
   photo's contour manually.
5. Under **3. Analysis parameters**, choose the sieve size threshold below
   which grains are counted as fine material.
6. Click **Process layer** to compute the coarse/fine split and geotechnical
   gradation parameters.
7. Click **Export all** to generate:
   - An Excel workbook with area/diameter data per grain (template layout).
   - A second Excel workbook with image size and material summary
     information.
   - A GeoPackage bundling the polygon layer, the source photo, and an
     embedded QGIS project — exported as-is in the project's coordinate
     system (no reprojection).

## License

Licensed under the GNU General Public License v3.0 — see [LICENSE](LICENSE)
for the full text.
