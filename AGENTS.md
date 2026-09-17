# Agent instructions

- Whenever you bump `version` in `ha_addon/zenproxy/config.yaml`, add a
  matching entry to `ha_addon/zenproxy/CHANGELOG.md` in the same commit —
  a `## [x.y.z] - YYYY-MM-DD` heading with one or two lines on what changed
  for a user of the addon. Home Assistant Supervisor renders that file in
  the addon's info tab, so it should read like release notes, not a diff.
