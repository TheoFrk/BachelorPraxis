import { test } from "node:test";
import assert from "node:assert/strict";
import { score, percent } from "./api.js";
test("missing signals stay missing and different score scales remain separate", () => {
  const item = {
    signals: {
      price: { score: 20, source: "S1" },
      finbert: { score: 0.2, source: "model" },
    },
  };
  assert.deepEqual(
    score(item, { price: true, finbert: true }).map((s) => s.score),
    [20, 0.2],
  );
  assert.deepEqual(score(item, { llm: true }), []);
  assert.equal(percent(null), "—");
});
