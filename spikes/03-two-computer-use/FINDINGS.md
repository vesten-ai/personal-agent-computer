# Spike 3 findings: one computer-use daemon for both model formats

**Answer: yes, one small daemon in the VM is enough.** GPT (gpt-6.1-sol) completed the desktop task
end to end through it, twice. The Claude path (`computer_toolset_20260801`) and OpenAI's built-in
`computer` tool are implemented against the documented wire formats and pass offline tests, but have
not run live yet: Claude was deferred, and the relay we used for GPT rejects the built-in tool.

Run 2026-10-13. The loop ran outside the VM (on a workstation), reached the VM's `pac-computerd`
through `incus exec`, and called the model through wodex's OpenAI-compatible relay.

## The task

Open Mousepad in an XFCE desktop, type `Personal Agent Computer spike 3 (openai)` and
`你好，世界。こんにちは。`, save as `/home/agent/Desktop/spike3-openai.txt`, close Mousepad.
The harness then reads the file back and checks that Mousepad is gone.

## Runs

| Run | Tool form | Result | Turns | Actions | Model time | Action time | Input tokens (cached) | Output tokens |
|-----|-----------|--------|-------|---------|------------|-------------|------------------------|---------------|
| 0 | built-in `computer` | **400** `Unsupported tool type: computer` from the relay | 0 | 0 | | | | |
| 1 | function tool | **pass**, file byte-exact, editor closed | 14 | 54 | 134 s | 117 s | 209,701 (0) | 2,486 |
| 2 | function tool, clipboard fix, `prompt_cache_key` | **pass**, file byte-exact, editor closed | 10 | 16 | 457 s | 28 s | 122,609 (90,240) | 1,182 |

Raw records: `results-2026-10-13-*.json` (actions and timings; screenshots not kept).

## What we learned

- **wodex relays GPT through Codex accounts, which refuse the built-in `computer` tool.** The adapter
  now has a third form, `tool_mode="function"`: the same action vocabulary offered as an ordinary
  function tool, answered with a `function_call_output` that carries the screenshot as `input_image`.
  GPT used it without trouble. This form should also work on any Responses-compatible relay or
  provider that supports function tools and image tool results.
- **Non-ASCII typing needs the clipboard path, and it had a bug.** `xclip -in` stays behind to serve
  the selection and held our stdout pipe, so the first two `type` actions with Chinese timed out after
  30 s. GPT recovered on its own by typing each character as a GTK Unicode code
  (Ctrl+Shift+U, hex, Enter), which is why run 1 needed 54 actions. Fixed in `image/computerd.py`
  (no pipes for xclip); run 2 typed the whole line in one action.
- **Prompt caching matters and needs a stable key.** Stateless replay resends the whole history every
  turn. Without a cache key nothing was cached; with `prompt_cache_key` 74% of input tokens were
  cache hits.
- **Model latency through the relay varied widely**: 6-8 s per turn in run 1, 20-78 s in run 2 with
  the same model and effort. The chat page needs visible progress between steps.
- **A simple desktop task costs about 120-210 k input tokens** at 1280×800 screenshots; each turn
  carries a fresh screenshot.

## How both formats map onto the daemon

| pac-computerd op | Anthropic toolset member | OpenAI action |
|------------------|--------------------------|---------------|
| `screenshot` | `screenshot` | `screenshot` (and after every call) |
| `zoom` | `zoom` | (none) |
| `click` (button, count, modifiers) | `left_click`, `right_click`, `middle_click`, `double_click`, `triple_click` | `click`, `double_click` |
| `move` | `mouse_move` | `move` |
| `drag` (path) | `left_click_drag` | `drag` |
| `mouse_down` / `mouse_up` | `left_mouse_down` / `left_mouse_up` | (none) |
| `scroll` (wheel clicks) | `scroll` (direction, amount) | `scroll` (pixel deltas, /100) |
| `type` | `type` | `type` |
| `key` (chord, repeat) | `key` (`ctrl+s`, repeat) | `keypress` (`["CTRL","S"]`) |
| `hold_key` | `hold_key` | (none) |
| `wait` | `wait` | `wait` |
| `cursor_position` | `cursor_position` | (none) |

The adapters differ only in wire format: tool declaration, how calls arrive (toolset members vs.
batched `actions` vs. function arguments), how results go back (`tool_result` with `toolset_name`
vs. `computer_call_output` vs. `function_call_output`) and how reasoning is replayed (signed thinking
blocks vs. encrypted reasoning items). The action set is the same.

## Still to do

- Run the Claude toolset live (deferred by decision on 2026-10-13).
- Run OpenAI's built-in `computer` tool against a provider that supports it.
- A harder task with Chromium (login wall, file download) and the safety-check flow.
