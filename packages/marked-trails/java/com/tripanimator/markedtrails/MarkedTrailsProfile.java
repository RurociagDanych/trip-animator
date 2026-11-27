package com.tripanimator.markedtrails;

import com.onthegomap.planetiler.FeatureCollector;
import com.onthegomap.planetiler.FeatureMerge;
import com.onthegomap.planetiler.Profile;
import com.onthegomap.planetiler.VectorTile;
import com.onthegomap.planetiler.reader.SourceFeature;
import com.onthegomap.planetiler.reader.osm.OsmElement;
import com.onthegomap.planetiler.reader.osm.OsmRelationInfo;
import java.util.List;

/**
 * Emits Marked Trail lines from OpenStreetMap route relations.
 *
 * <p>The YAML custom schema cannot read the tags of a parent relation for a member way, so the
 * trail type, network, colour, ref and name come from the relation, collected on the first pass.
 */
public class MarkedTrailsProfile implements Profile {

  static final String LAYER = "marked_trails";
  static final int MIN_ZOOM = 5;
  static final int LOCAL_MIN_ZOOM = 9;
  static final int MAX_ZOOM = 13;
  static final int LABEL_MIN_ZOOM = 13;

  // Kept small: one instance per route relation is held in memory during the second pass.
  private record MarkedTrailRelation(
    @Override long id,
    String type,
    String network,
    String colour,
    String ref,
    String name,
    int minZoom
  ) implements OsmRelationInfo {}

  @Override
  public List<OsmRelationInfo> preprocessOsmRelation(OsmElement.Relation relation) {
    if (!relation.hasTag("type", "route")) {
      return null;
    }
    String type = trailType(relation.getString("route", ""));
    if (type == null) {
      return null;
    }
    String network = networkLevel(relation.getString("network", ""));
    return List.of(new MarkedTrailRelation(
      relation.id(),
      type,
      network,
      relation.getString("colour", ""),
      relation.getString("ref", ""),
      relation.getString("name", ""),
      "local".equals(network) ? LOCAL_MIN_ZOOM : MIN_ZOOM
    ));
  }

  @Override
  public void processFeature(SourceFeature sourceFeature, FeatureCollector features) {
    if (!sourceFeature.canBeLine()) {
      return;
    }
    for (var member : sourceFeature.relationInfo(MarkedTrailRelation.class, true)) {
      MarkedTrailRelation trail = member.relation();
      features.line(LAYER)
        .setAttr("type", trail.type())
        .setAttr("network", trail.network())
        .setAttr("colour", trail.colour())
        .setAttrWithMinzoom("ref", trail.ref(), LABEL_MIN_ZOOM)
        .setAttrWithMinzoom("name", trail.name(), LABEL_MIN_ZOOM)
        .setZoomRange(trail.minZoom(), MAX_ZOOM)
        .setMinPixelSize(0);
    }
  }

  @Override
  public List<VectorTile.Feature> postProcessLayerFeatures(String layer, int zoom,
    List<VectorTile.Feature> items) {
    return FeatureMerge.mergeLineStrings(items, 0.5, 0.1, 4);
  }

  @Override
  public String name() {
    return "Marked Trails";
  }

  @Override
  public String description() {
    return "Hiking, cycling and MTB marked trails from OpenStreetMap route relations";
  }

  @Override
  public boolean isOverlay() {
    return true;
  }

  @Override
  public String attribution() {
    return "<a href=\"https://www.openstreetmap.org/copyright\" target=\"_blank\">&copy; OpenStreetMap contributors</a>";
  }

  /** Folds `foot` into `hiking`, the only trail type the app reads in this build. */
  static String trailType(String route) {
    return switch (route) {
      case "hiking", "foot" -> "hiking";
      case "bicycle" -> "cycling";
      case "mtb" -> "mtb";
      default -> null;
    };
  }

  /** Maps the OSM network prefix to a level; hiking networks (iwn, nwn, rwn, lwn) share the level. */
  static String networkLevel(String network) {
    return switch (network) {
      case "icn", "iwn" -> "international";
      case "ncn", "nwn" -> "national";
      case "rcn", "rwn" -> "regional";
      case "lcn", "lwn" -> "local";
      default -> "";
    };
  }
}
