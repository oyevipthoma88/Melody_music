# Bot maintenance rules
- Stream replacements share per-chat commit locks and generation fences.
- Speed is a stream-builder input and commits only after successful replacement.
- Shared downloads track consumer owners and promote interactive waiters; reclaim only granted slots.
- Cache missing settings with a sentinel and report failed persistent writes.
