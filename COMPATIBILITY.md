# Compatibility

Current releases support Apple Silicon only. Intel Macs and execution under Rosetta are outside the supported scope.

Compatibility is determined by schema validation and native staging checks, not an exact version allowlist. An unknown future format is **not** automatically supported.

## Evidence

- Local machine: Apple Silicon, macOS 26.6.2; Arc 1.169.0 / 1.170.0 source schemas inspected; Zen 1.23.4b (Gecko 158.0.1) native synthetic tests.
- Installer runtime: uv 0.13.5, managed CPython 3.12.12; complete dependency hashes in `uv.lock`. Minimum runtime macOS 11; installed browser requirements can be higher.
- Local native tests and an isolated bootstrap have passed on the current Apple Silicon Mac. The release workflow runs the same native synthetic suite and installer checks on a fresh GitHub-hosted `macos-15` arm64 runner. See the commit’s Actions result for hosted evidence. This is a disposable CI machine, not an additional physical laptop. Intel is outside the support scope.
- Older/newer Arc or Zen versions: unverified until their formats and the installed native APIs pass local staging checks. Applications are never modified or updated by this tool.

## Adapters and gates

- Arc JSON v1 Sidebar/Windows/archive; mandatory identities/references validated. Chromium SNSS generations and SQLite databases are read separately. Locked/corrupt mandatory data fails closed.
- macOS Arc OSCrypt v10 AES-CBC via Keychain and the system CommonCrypto library (tested against the NIST CBC vector). No Rust/compiler or external crypto package is needed. Unknown password cipher fails closed. Unsupported optional encrypted cookies are skipped.
- Gecko SessionStore and ContextualIdentityService module locations are selected from known candidates. Workspace/session schema must round-trip unchanged through two native restarts.
- Cookie expiration unit is probed by writing an expiring synthetic cookie and checking native storage. Context, partition key and security flags are verified after restart.
- The 0.4.2 preview fixes ordinary domain-cookie imports and records per-record skip reasons only with `--diagnostics`. Cross-site-ancestor CHIPS remain skipped: native key storage was probed, but equivalent request isolation across Chromium and Gecko has not yet been validated. No partition key is flattened.
- Diagnostic/native regression checks include 5016 source cookies across two containers, domain versus host-only cookies, native quota eviction detection, and three Spaces with editable restored themes. An unknown Arc theme remains an explicit skip with native Zen defaults, not a claim of successful theme migration.
- Appearance: native theme limits and selectable SVG loading are probed. Shared icon mapping covers Spaces and nested folders; folder emoji are unsupported by the verified SVG-only picker. Unknown optional appearance formats preserve existing values. Appearance-only repair installs patched original sessions and retains all other files byte-for-byte.
- Space UI switching and container-specific Essentials are checked in the temporary browser's actual DOM and Zen manager. The user browser never receives debug flags.
- Before opening a copied Zen profile, its registration StoreID/preferences are removed from the copy so Gecko cannot relocate the original registration. Full source-root fingerprints are checked around repair.
- Repair exports NSS logins only to process memory, current Places visits/bookmarks, cookies and session state from private copies. Conflicting login passwords, conflicting multi-window navigation, preexisting nondefault containers, and encrypted Zen payment cards stop repair before replacing anything.
- Full current Zen root is backed up; external profile directories are rejected. Atomic rename requires the same filesystem. Free space and process access are checked before preparation.

Native Zen source defining Space Profile semantics: [Space creation](https://github.com/zen-browser/desktop/blob/1.23.4b/src/zen/spaces/ZenSpaceCreation.mjs), [Space manager](https://github.com/zen-browser/desktop/blob/1.23.4b/src/zen/spaces/ZenSpaceManager.mjs). Gecko automation: [Marionette](https://firefox-source-docs.mozilla.org/remote/marionette/index.html).
