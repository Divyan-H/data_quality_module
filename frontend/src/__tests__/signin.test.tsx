import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, AUTH_EVENT } from "../api";
import { SignIn } from "../components/SignIn";

const reply = (status: number, body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));

afterEach(() => vi.unstubAllGlobals());

describe("access token sign-in", () => {
  it("announces a 401 auth_required so the app can show the sign-in screen", async () => {
    vi.stubGlobal("fetch", vi.fn(() => reply(401, { error: { code: "auth_required", message: "Sign in first." } })));
    const heard = vi.fn();
    window.addEventListener(AUTH_EVENT, heard);
    await api.listRuns().catch(() => undefined);
    window.removeEventListener(AUTH_EVENT, heard);
    expect(heard).toHaveBeenCalledTimes(1);
  });

  it("posts the trimmed token and reports success", async () => {
    const spy = vi.fn(() => reply(200, { auth_required: true, signed_in: true }));
    vi.stubGlobal("fetch", spy);
    const done = vi.fn();
    render(<SignIn onSignedIn={done} />);
    const button = screen.getByRole("button", { name: "Sign in" });
    expect(button).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Access token"), "  s3cret-token-0123456  ");
    await userEvent.click(button);
    expect(done).toHaveBeenCalled();
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/session$/);
    expect(JSON.parse(String(init.body))).toEqual({ token: "s3cret-token-0123456" });
  });

  it("shows the service's message when the token is wrong", async () => {
    vi.stubGlobal("fetch", vi.fn(() => reply(401, { error: { code: "bad_token", message: "That access token is not right." } })));
    const done = vi.fn();
    render(<SignIn onSignedIn={done} />);
    await userEvent.type(screen.getByLabelText("Access token"), "wrong-token-000000");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("That access token is not right.")).toBeInTheDocument();
    expect(done).not.toHaveBeenCalled();
  });
});
