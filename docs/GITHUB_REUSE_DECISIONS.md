# GitHub Reuse Decisions

## Adopt patterns / compatible libraries

- FastAPI full-stack template: architecture inspiration; our scaffold remains minimal.
- Satori + resvg-js: recommended Phase 1 creative renderer.
- rembg: optional background removal, with quality fallback to original product image.
- imagehash: perceptual near-duplicate detection.
- Pinterest official SDK/generated API client: official provider reference.
- Celery: Phase 2 only.

## Reference only

- Postiz: excellent Pinterest workflow reference, especially unknown-outcome handling. AGPL-3.0 means substantial code should not be copied into this proprietary/internal codebase without deliberate license acceptance.
