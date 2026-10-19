// drydocc lab: stop a model that repeats one tool call instead of finishing.
//
// A weak model can loop on the same call — `echo "DONE"` hundreds of times in
// one turn — and burn a free tier's shared daily cap. The fifth identical call
// in a row is refused with a hint; the tenth aborts the turn and leaves a
// marker on stderr, which pi.sh turns into a failed step.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const REFUSE_AT = 5;
const ABORT_AT = 10;

export default function (pi: ExtensionAPI) {
  let last = "";
  let repeats = 0;
  pi.on("tool_call", async (event, ctx) => {
    if (event.parentToolCallId) return; // nested calls belong to their parent
    const call = `${event.toolName}:${JSON.stringify((event as { input?: unknown }).input ?? null)}`;
    repeats = call === last ? repeats + 1 : 1;
    last = call;
    if (repeats >= ABORT_AT) {
      console.error(`loop-guard: aborted after ${repeats} identical ${event.toolName} calls`);
      ctx.abort();
      return { block: true, reason: "aborted: the same call repeated" };
    }
    if (repeats >= REFUSE_AT) {
      return {
        block: true,
        reason:
          `You made this identical ${event.toolName} call ${repeats} times in a row; it will not run again. ` +
          "If the task is finished, end your turn with your final answer as plain text, not a tool call.",
      };
    }
  });
}
