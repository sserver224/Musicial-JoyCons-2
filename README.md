# Musicial-JoyCons-2
Play music on JoyCons and ProCons, including drums on Linux.
Inspired by [sarossilli/MusicalJoycons](https://github.com/sarossilli/MusicalJoycons).

Supports up to 6 logical actuators.
Unlike the other projects, this takes in FamiStudio text. This requires:
- FamiStudio tempo mode
- NTSC export.

The only expansion card supported is the MMC5.
Test sounds for these channels are:
1, 2, 5, 6: tone at the master pitch.
3: A tone one octave below the master pitch.
4: A burst of noise.

Drums are supported but I tried my best to emulate the noise channel.

Requirements: `hidapi`
Optional: `customtkinter` (for the modern GUI)
