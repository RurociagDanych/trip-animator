package com.tripanimator.markedtrails;

import com.onthegomap.planetiler.Planetiler;
import com.onthegomap.planetiler.config.Arguments;
import java.nio.file.Path;

/**
 * Command line entry point; the Python CLI passes `--osm_path`, `--output` and `--tmpdir`.
 */
public final class MarkedTrailsMain {

  private MarkedTrailsMain() {
  }

  public static void main(String[] args) throws Exception {
    Planetiler.create(Arguments.fromArgsOrConfigFile(args))
      .setProfile(new MarkedTrailsProfile())
      .addOsmSource("osm", Path.of("data", "sources", "extract.osm.pbf"))
      .overwriteOutput(Path.of("data", "marked-trails.pmtiles"))
      .run();
  }
}
