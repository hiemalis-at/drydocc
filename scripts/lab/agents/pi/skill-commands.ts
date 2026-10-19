// drydocc lab: canonical `/<skill> args` invocation for pi.
//
// pi exposes a skill only as `/skill:<name>`; drydocc prompts, scenarios and
// docs say `/<name>`, as every other agent does. pi runs extension `input`
// handlers before it expands `/skill:` commands, so rewriting the prefix here
// hands the rest to pi's own skill expansion. Registered commands are
// dispatched before input handlers, so none of them can be shadowed; text
// whose first word names no skill passes through unchanged.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  pi.on("input", async (event) => {
    const m = /^\/([A-Za-z0-9][\w.-]*)(\s[\s\S]*)?$/.exec(event.text);
    if (!m) return { action: "continue" };
    const skill = `skill:${m[1]}`;
    const known = pi.getCommands().some((c) => c.source === "skill" && c.name === skill);
    if (!known) return { action: "continue" };
    return { action: "transform", text: `/${skill}${m[2] ?? ""}` };
  });
}
