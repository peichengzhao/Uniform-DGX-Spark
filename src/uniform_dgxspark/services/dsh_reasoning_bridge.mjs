import { appendFileSync } from "node:fs";

export const name = "uniform-reasoning-bridge";

export function apply(ctx, config) {
  const path = typeof config?.path === "string" ? config.path : "";
  const sessionId = typeof config?.sessionId === "string" ? config.sessionId : "";
  if (!path || !sessionId) return;

  appendFileSync(path, '{"type":"bridge-ready"}\n', { encoding: "utf8", mode: 0o600 });
  const attempts = new Map();
  ctx.on("agent/assistant-stream", ({ agent, frame }) => {
    if (String(agent?.session?.id ?? "") !== sessionId) return;
    if (frame?.type === "start") {
      attempts.set(String(frame.attemptId), {
        turn: frame.turn,
        step: frame.step,
      });
      appendFileSync(
        path,
        `${JSON.stringify({
          type: "attempt-start",
          attemptId: String(frame.attemptId),
          turn: frame.turn,
          step: frame.step,
        })}\n`,
        { encoding: "utf8", mode: 0o600 },
      );
      return;
    }
    if (frame?.type === "end") {
      attempts.delete(String(frame.attemptId));
      return;
    }
    if (frame?.type !== "chunk" || frame.chunk?.type !== "reasoning-delta") return;
    const attemptId = String(frame.attemptId);
    const coordinate = attempts.get(attemptId);
    if (!coordinate || typeof frame.chunk.text !== "string" || !frame.chunk.text) return;
    appendFileSync(
      path,
      `${JSON.stringify({
        attemptId,
        turn: coordinate.turn,
        step: coordinate.step,
        index: frame.chunk.index,
        text: frame.chunk.text,
      })}\n`,
      { encoding: "utf8", mode: 0o600 },
    );
  }, { global: true });
}
