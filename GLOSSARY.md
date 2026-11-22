# Activity Library

The personal activity-history area of Trip Animator. The English product name is provisional.

## Language

**Activity Library**:
A user's private collection of sporting activities, explored through search, a map, and statistics.
_Avoid_: Atlas, import screen

**Historical Import**:
The initial collection of a user's available activity history from a connected provider.
_Avoid_: Import all (when only a bounded batch is imported)

**Library Activity**:
A single sporting session in a user's activity history, whether or not it has a geographic track or is used in a trip. Using it in several projects does not create additional library activities.
_Avoid_: Trip, stage

**Atlas**:
The view of curated trips and journeys prepared in the portal, rather than the user's entire sporting history.
_Avoid_: Activity Library, training history

**Project-local Split**:
A division of an activity's material into segments for one trip or animation project. It does not divide the library activity or change its use in other projects.
_Avoid_: Splitting the library activity

## Trip Playback

**Cinematic Photo Stop**:
A moment in a trip film when the route marker stops, the map zooms in and gives way to one or more full-frame photos, and the journey resumes after the return to the map.
_Avoid_: Cinematic Ken Burns (a photo card style)

**Photo Group**:
A sequence of route photos shown in a single Cinematic Photo Stop because each consecutive pair is within the chosen marker travel-time threshold.
