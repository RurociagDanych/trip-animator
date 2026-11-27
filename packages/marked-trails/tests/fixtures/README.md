# Marked Trails test fixtures

`andorra-marked-trails.osm.pbf` is a small OpenStreetMap extract for the Docker test of the builder image. Data © OpenStreetMap contributors, available under the [Open Database License](https://www.openstreetmap.org/copyright).

It was cut from the Geofabrik Andorra extract of 2026-11-26 with the osmium of the builder image:

```bash
osmium tags-filter -o routes.osm.pbf andorra-latest.osm.pbf r/route=hiking,foot,bicycle,mtb
osmium extract -s complete_ways -b 1.55,42.50,1.65,42.58 \
  --output-header=osmosis_replication_timestamp=2026-11-26T21:21:01Z \
  -o andorra-marked-trails.osm.pbf routes.osm.pbf
```

It holds 22 relations that match the filter. Twenty of them are `type=route`: hiking on `iwn`, `nwn` and `lwn` networks and one without a network tag, cycling and MTB on `lcn`. The other two are `type=superroute`, which the profile ignores.
