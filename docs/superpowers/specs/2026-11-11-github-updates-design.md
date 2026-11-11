# GitHub launcher updates

The user authorized connecting the installed launcher to the published public GitHub repository and continuing its build/install/release workflow. Version 3.41.31 will use `abdulmunthakimwork/minecraft-launcher` as its default update source.

Use the existing electron-updater 6.8.11 public GitHub provider. It discovers stable releases, reads `latest.yml`, and downloads the full NSIS installer. Preserve startup/six-hour checking, automatic verified downloading, explicit Restart and install, and lifecycle admission. Retain custom generic HTTPS directories and persisted user source choices. Missing config defaults to GitHub; an explicitly saved empty source still disables checks. The installed PC will be configured to GitHub as part of this requested setup.

Approaches considered: a separate generic host would need another service; a custom release downloader would duplicate the tested update engine. The existing GitHub provider is the smallest supported integration. Its network policy permits the fixed repository's discovery and stable setup/metadata paths plus GitHub's release CDN scoped to the verified numeric repository ID. Reject other repositories, HTTP downgrades, credentialed URLs, encoded path escapes and mismatched version/tag/installer metadata. Keep size/SHA-512 checks and full-download mode.

Settings will explain automatic checking/downloading and the explicit restart action, show the default repository source, and provide a Use GitHub action while preserving the editable custom-source field. The existing Launcher updates entry will indicate when a verified update is ready; one state subscription must survive Settings remounts without retaining detached controls.

Preserve accounts, profiles, mods and original Git staging. Commit and publish only the curated public source. Reuse sanitized media and unchanged frozen Python host in a new release staging folder. Publish a new immutable v3.41.31 release after content/privacy and updater tests pass. Test the actual GitHub engine and installed app; don't release a dummy future version just for testing.
