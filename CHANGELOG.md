# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-11-21

### Added

- Photo image on parcel devices: the picture the Post takes of the parcel on the sorting conveyor.

### Changed

- Requires `post-at-lib` 0.2.0.

## [0.1.1] - 2026-11-21

### Added

- Location sensor on every parcel device: the place, postal code and country of the latest tracking
  event that has one.

## [0.1.0] - 2026-11-19

### Added

- Initial release.
- Config flow with e-mail/password sign-in and a browser sign-in fallback for MFA and other prompts;
  only the session is stored, never the password.
- Re-authentication flow and options for the update interval and how long delivered shipments are kept.
- Sensors for incoming, outgoing and ready-for-pickup shipments, plus one enum sensor per active shipment.
- `post_at_shipment_updated` event when a shipment's tracking state changes.
- Actions `post_at.redirect_shipment`, `post_at.reset_redirection`, `post_at.get_redirection_options`,
  `post_at.add_shipment`, `post_at.remove_shipment`, `post_at.rename_shipment` and `post_at.track_shipment`.
- Diagnostics with tokens and personal data redacted.

[0.2.0]: https://github.com/ch-rit/ha-post-at/releases/tag/v0.2.0
[0.1.1]: https://github.com/ch-rit/ha-post-at/releases/tag/v0.1.1
[0.1.0]: https://github.com/ch-rit/ha-post-at/releases/tag/v0.1.0
