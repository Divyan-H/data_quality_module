import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../api";

const reply = (status: number, body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));

afterEach(() => vi.unstubAllGlobals());

describe("api client", () => {
  it("returns the parsed body", async () => {
    vi.stubGlobal("fetch", vi.fn(() => reply(200, [{ id: "a" }])));
    expect(await api.listRuns()).toEqual([{ id: "a" }]);
  });

  it("turns the service's error body into an ApiError with its extras", async () => {
    vi.stubGlobal("fetch", vi.fn(() => reply(409, { error: { code: "llm_unavailable", message: "Ollama is not reachable.", llm: { available: false } } })));
    const err = await api.start("r", "A").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 409, code: "llm_unavailable", message: "Ollama is not reachable.", extra: { llm: { available: false } } });
  });

  it("explains a validation failure and an unexpected status without leaking raw JSON", async () => {
    vi.stubGlobal("fetch", vi.fn(() => reply(422, { detail: [{ msg: "bad" }] })));
    expect(await api.start("r", "A").catch((e) => e)).toMatchObject({ code: "bad_request" });
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response("<html>boom</html>", { status: 502, statusText: "Bad Gateway" }))));
    expect((await api.listRuns().catch((e) => e)).message).toBe("The service answered 502 Bad Gateway.");
  });

  it("reports an unreachable service", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    expect(await api.listRuns().catch((e) => e)).toMatchObject({ status: 0, code: "unreachable" });
  });

  it("handles 204", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(null, { status: 204 }))));
    expect(await api.deleteRun("r")).toBeUndefined();
  });

  it("sends files, their paths and the LLM choice as a form", async () => {
    const spy = vi.fn(() => reply(201, { id: "r" }));
    vi.stubGlobal("fetch", spy);
    await api.createRun([{ file: new File(["x"], "a.csv"), path: "Client/a.csv" }], "My run", "on");
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    const form = init.body as FormData;
    expect(url).toBe("/api/v1/runs");
    expect(form.getAll("paths")).toEqual(["Client/a.csv"]);
    expect(form.get("name")).toBe("My run");
    expect(form.get("llm")).toBe("on");
    expect((form.get("files") as File).name).toBe("a.csv");
  });

  it("starts a phase with JSON options", async () => {
    const spy = vi.fn(() => reply(200, {}));
    vi.stubGlobal("fetch", spy);
    await api.start("r", "B", { llm: "off", fixes: { "D-14": false } });
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/runs/r/phases/B/start");
    expect(JSON.parse(init.body as string)).toEqual({ llm: "off", fixes: { "D-14": false } });
  });

  it("builds download links without calling the service", () => {
    expect(api.fileUrl("r", "structural-review")).toBe("/api/v1/runs/r/files/structural-review");
    expect(api.bundleUrl("r")).toBe("/api/v1/runs/r/bundle");
    expect(api.templateUrl("r", "C", "a:0:t1")).toBe("/api/v1/runs/r/phases/C/config-template?table=a%3A0%3At1");
    expect(api.templateUrl("r", "D")).toBe("/api/v1/runs/r/phases/D/config-template");
  });
});
