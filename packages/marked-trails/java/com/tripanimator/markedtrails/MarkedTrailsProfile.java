package com.tripanimator.markedtrails;

import com.onthegomap.planetiler.FeatureCollector;
import com.onthegomap.planetiler.FeatureMerge;
import com.onthegomap.planetiler.Profile;
import com.onthegomap.planetiler.VectorTile;
import com.onthegomap.planetiler.reader.SourceFeature;
import com.onthegomap.planetiler.reader.osm.OsmElement;
import com.onthegomap.planetiler.reader.osm.OsmRelationInfo;
import java.util.List;
import java.util.concurrent.atomic.LongAdder;

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

  // Planetiler preprocesses relations on several threads.
  private final LongAdder markedTrailRelationCount = new LongAdder();

  @Override
  public List<OsmRelationInfo> preprocessOsmRelation(OsmElement.Relation relation) {
    if (!relation.hasTag("type", "route")) {
      return null;
    }
    String type = markedTrailType(relation.getString("route", ""));
    if (type == null) {
      return null;
    }
    String network = networkLevel(relation.getString("network", ""));
    markedTrailRelationCount.increment();
    return List.of(new MarkedTrailRelation(
      relation.id(),
      type,
      network,
      nullIfEmpty(relation.getString("colour", "")),
      nullIfEmpty(relation.getString("ref", "")),
      nullIfEmpty(relation.getString("name", "")),
      network == null || "local".equals(network) ? LOCAL_MIN_ZOOM : MIN_ZOOM
    ));
  }

  @Override
  public void processFeature(SourceFeature sourceFeature, FeatureCollector features) {
    if (!sourceFeature.canBeLine()) {
      return;
    }
    for (var member : sourceFeature.relationInfo(MarkedTrailRelation.class, true)) {
      MarkedTrailRelation markedTrail = member.relation();
      features.line(LAYER)
        .setAttr("type", markedTrail.type())
        .setAttr("network", markedTrail.network())
        .setAttr("colour", markedTrail.colour())
        .setAttrWithMinzoom("ref", markedTrail.ref(), LABEL_MIN_ZOOM)
        .setAttrWithMinzoom("name", markedTrail.name(), LABEL_MIN_ZOOM)
        .setZoomRange(markedTrail.minZoom(), MAX_ZOOM)
        .setMinPixelSize(0);
    }
  }

  @Override
  public List<VectorTile.Feature> postProcessLayerFeatures(String layer, int zoom,
    List<VectorTile.Feature> items) {
    return FeatureMerge.mergeLineStrings(items, 0.5, 0.1, 4);
  }

  /** Number of route relations accepted as Marked Trails so far. */
  public long markedTrailRelationCount() {
    return markedTrailRelationCount.sum();
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

  /** Planetiler drops null attributes, so absent OSM tags leave no attribute in the tile. */
  static String nullIfEmpty(String value) {
    return value.isEmpty() ? null : value;
  }

  /** Maps an OSM route value to a trail type; `foot` is folded into `hiking`. */
  static String markedTrailType(String route) {
    return switch (route) {
      case "hiking", "foot" -> "hiking";
      case "bicycle" -> "cycling";
      case "mtb" -> "mtb";
      default -> null;
    };
  }

  /**
   * Maps the OSM network prefix to a level; hiking networks (iwn, nwn, rwn, lwn) share the level.
   * A missing or unknown network returns null and is drawn from the local zoom, so untagged
   * routes do not crowd the wide views.
   */
  static String networkLevel(String network) {
    return switch (network) {
      case "icn", "iwn" -> "international";
      case "ncn", "nwn" -> "national";
      case "rcn", "rwn" -> "regional";
      case "lcn", "lwn" -> "local";
      default -> null;
    };
  }
}
