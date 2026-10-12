# Changelog

All notable changes to this integration. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.6.1] - 2026-10-12

### Fixed

- The history import no longer stops for good at a bad reading in the vendor's data. A one-hour `0`,
  a short dip or a spike is skipped; a change that lasts, such as a replaced meter, becomes the new
  baseline instead of being counted as usage. Previously a single falling reading halted every run
  at the same hour, so an affected meter never got any statistics.
  ([#4](https://github.com/kurtsson/hass-perific/issues/4),
  [#5](https://github.com/kurtsson/hass-perific/pull/5) — thanks
  [@GV94](https://github.com/GV94))
- `import_history` with a start time continues from the stored hour just before it, so the rebuilt
  hours line up with the rest of the series.

## [0.6.0] - 2026-10-12

### Changed

- The account password is stored in the config entry again, next to the API token. The API revokes
  tokens well before their nominal one-year expiry, and without the password every revocation meant
  a reauthentication prompt and missing statistics until someone typed it in. A rejected or expired
  token is now replaced automatically.
- Reauthentication is only requested when the password itself is rejected, or after five
  consecutive failed polls (up from three).
- Entries set up by 0.3.0 to 0.5.2 hold no password. They ask for it once, the next time their
  token is rejected, and renew by themselves from then on.

### Fixed

- The hourly history import kept calling the API with a revoked token and logged a full traceback
  every hour. It now renews the token the same way polling does, and a rejection that survives
  renewal is logged as a single warning.
- Saving a renewed token no longer reloads the integration in the middle of a poll.

## [0.5.2] - 2026-09-22

### Fixed

- Solar revenue calculation.

## [0.5.1] - 2026-09-22

### Added

- Savings from self-consumed solar, from a production statistic chosen in the options.
- `get_dashboard` service returning a ready-made dashboard for the series this instance writes.

## [0.5.0] - 2026-09-21

### Added

- Optional cost and export-compensation series, from a price entity and tariff set in the options.

## [0.4.0] - 2026-09-21

### Changed

- Hourly energy is imported into long-term statistics from the vendor's history endpoint, which
  fills gaps left while Home Assistant was not running.

### Added

- `import_history` service to rebuild the imported series from a given time.

## [0.3.1] - 2026-09-21

### Fixed

- A single rejected token no longer stops collection; reauthentication waits for three consecutive
  rejections.

## [0.3.0] - 2026-09-17

### Changed

- The password is exchanged for an API token at setup and no longer stored. Existing entries
  migrate automatically.

### Added

- Status and last-packet sensors.

## [0.2.0] - 2026-09-16

### Added

- Diagnostics download, configurable poll interval, and a Repairs issue when the API rate limits.

[0.6.1]: https://github.com/kurtsson/hass-perific/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/kurtsson/hass-perific/compare/v0.5.2...v0.6.0
[0.5.2]: https://github.com/kurtsson/hass-perific/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/kurtsson/hass-perific/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/kurtsson/hass-perific/compare/0.2.0...v0.5.0
[0.4.0]: https://github.com/kurtsson/hass-perific/commit/01e0e4b
[0.3.1]: https://github.com/kurtsson/hass-perific/commit/485134d
[0.3.0]: https://github.com/kurtsson/hass-perific/commit/1771da2
[0.2.0]: https://github.com/kurtsson/hass-perific/releases/tag/0.2.0
