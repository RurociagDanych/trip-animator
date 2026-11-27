package com.tripanimator.markedtrails;

import com.onthegomap.planetiler.Planetiler;
import com.onthegomap.planetiler.config.Arguments;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * Command line entry point; the Python CLI passes `--osm_path`, `--output`, `--tmpdir` and
 * `--marked_trail_count_path`, the file that receives the number of Marked Trail relations.
 */
public final class MarkedTrailsMain {

  private MarkedTrailsMain() {
  }

  public static void main(String[] args) throws Exception {
    Arguments arguments = Arguments.fromArgsOrConfigFile(args);
    String countPath = arguments.getString(
      "marked_trail_count_path", "file to write the Marked Trail relation count to", "");
    MarkedTrailsProfile profile = new MarkedTrailsProfile();
    Planetiler.create(arguments)
      .setProfile(profile)
      .addOsmSource("osm", Path.of("data", "sources", "extract.osm.pbf"))
      .overwriteOutput(Path.of("data", "marked-trails.pmtiles"))
      .run();
    if (!countPath.isEmpty()) {
      Files.writeString(
        Path.of(countPath), profile.markedTrailRelationCount() + "\n", StandardCharsets.UTF_8);
    }
  }
}
