# Edifice places (optional)

Add `{group}.geojson` here to place an edifice on a map, where `{group}` is the identifier
prefix shared by its images (`1_4_1_7_a` → `1_4_1_7.geojson`).

The file can be a bare geometry, a Feature or a FeatureCollection:

```json
{ "type": "Point", "coordinates": [-118.0, 34.0] }
```

Coordinates are `[longitude, latitude]`. The build writes the places as IIIF
[navPlace](https://iiif.io/api/extension/navplace/) on the edifice manifest, its image
manifests, and the category and all-images manifests. Clover shows them on a map: the
Viewer adds a Map tab, and edifice pages get a Location map. A `properties.label` is
optional; the edifice name is used if it is missing. Edifices without a file get no map.

## Properties

These optional Feature `properties` become metadata on the edifice manifest, its image
manifests, and the edifice and image pages. Anything else is kept in the GeoJSON but not shown.

| Property | Shown as | Example |
| --- | --- | --- |
| `name` | Place name (and the map label) | `"George Sturges House"` |
| `address` | Address | `"651 Warner Avenue, Los Angeles, California"` |
| `completed` | Completed | `"1942–1943"` |
| `client` | Client | `"George D. Sturges"` |
| `style` | Architectural style | `"Usonian"` |
| `status` | Status | `"Demolished by 1981"` |
| `designations` | Designations (string or list) | `["Los Angeles Historic-Cultural Monument No. 174"]` |
| `precision` | `"approximate"` adds "(approximate)" to Coordinates | `"approximate"` |
| `source` | Sources, as links (string or list of URLs) | `["https://en.wikipedia.org/wiki/Strathmore_Apartments"]` |

Coordinates are shown for Point geometries as `latitude, longitude`.
