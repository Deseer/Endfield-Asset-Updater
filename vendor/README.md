# Vendored dependencies

`EndfieldStudio/` is a source snapshot based on
[`EIHRTeam/EndfieldStudio`](https://github.com/EIHRTeam/EndfieldStudio), including
the local CLI, VFS, PCK and audio-map changes required by this exporter. It is
kept directly in this repository so a build does not depend on another local
Git worktree or uncommitted submodule state.

When updating it, compare against the recorded upstream commit
`ccf769b1a3bedd9e300e0b1ba9c87ec1b87b2695` and preserve the local changes
deliberately.
