# ArcToZen interface preview

Captured from the actual terminal wizard in `--demo --dry-run` mode, using synthetic profiles and records. No real browser data, credentials or backups are shown. These captures are for release review; the public installation URL is not live yet.

## Walkthrough

![From Arc to Zen, with a backup kept for recovery](assets/walkthrough.gif)

## Startup

Bring your browser with you.

![ArcToZen startup](assets/startup.png)

## Browsers

Review the detected browsers before continuing.

![Browser detection](assets/browsers.png)

## Options

Choose the profiles, Spaces and data you want to bring.

![Migration options](assets/options.png)

## Scanning

Scan first. Review before replacing anything.

![Scanning Arc](assets/scanning.png)

## Review

See what will move before you confirm.

![Review and replacement warning](assets/review.png)

## Transfer

Follow each stage while ArcToZen prepares and verifies your data.

![Migration progress](assets/transfer.png)

## Completion

Finish and open Zen, restore a backup, or finish and open GitHub.

![Migration complete](assets/success.png)

## Reproduce the capture

`scripts/capture_readme.py` launches only the synthetic wizard in a controlling PTY, decodes its actual ANSI output, and renders the terminal cells with Menlo. Developer-only dependencies are Pillow 13.0.0 and pyte 0.8.2; these are not migration runtime dependencies. Screens use a 104 × 42 terminal. The GIF is sampled at up to eight frames per second.

The capture script never opens real browsers, reads browser profiles or follows the final GitHub action. It exits the mock wizard with Esc after recording the completion screen.
