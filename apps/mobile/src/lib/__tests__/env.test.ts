/**
 * EXPO_PUBLIC_API_URL scheme validation (issue #292), mirroring
 * apps/api/app/config.py's `_require_non_local_dashboard_origins_in_
 * production` test coverage (#255) on this side of the fence.
 *
 * `__DEV__` is a real, writable RN/Jest global (`true` by default under
 * jest-expo): set and restored around each test rather than mocked, so
 * `env.apiUrl`'s own `isProductionBuild()` check is exercised for real,
 * not stubbed out.
 */
import { env } from "../env";

const ORIGINAL_API_URL = process.env.EXPO_PUBLIC_API_URL;
const ORIGINAL_DEV = (globalThis as { __DEV__?: boolean }).__DEV__;

function setDev(value: boolean) {
  (globalThis as { __DEV__?: boolean }).__DEV__ = value;
}

afterEach(() => {
  process.env.EXPO_PUBLIC_API_URL = ORIGINAL_API_URL;
  setDev(ORIGINAL_DEV ?? true);
});

describe("env.apiUrl, production build", () => {
  it("rejects a plaintext http:// origin", () => {
    setDev(false);
    process.env.EXPO_PUBLIC_API_URL = "http://api.stoop.example";
    expect(() => env.apiUrl).toThrow(/https/i);
  });

  it("accepts an https:// origin", () => {
    setDev(false);
    process.env.EXPO_PUBLIC_API_URL = "https://api.stoop.example";
    expect(env.apiUrl).toBe("https://api.stoop.example");
  });

  it("rejects the unset default (which is itself plaintext localhost)", () => {
    setDev(false);
    delete process.env.EXPO_PUBLIC_API_URL;
    expect(() => env.apiUrl).toThrow(/https/i);
  });

  it("rejects a scheme-less / malformed value", () => {
    setDev(false);
    process.env.EXPO_PUBLIC_API_URL = "api.stoop.example";
    expect(() => env.apiUrl).toThrow(/https/i);
  });

  it("never echoes the full configured value in the thrown message (rule 5: it can carry credentials)", () => {
    setDev(false);
    process.env.EXPO_PUBLIC_API_URL = "http://secret-token@api.stoop.example";
    expect(() => env.apiUrl).toThrow(/https/i);
    try {
      void env.apiUrl;
      throw new Error("expected env.apiUrl to throw");
    } catch (error) {
      expect(String(error)).not.toContain("secret-token");
      expect(String(error)).not.toContain("api.stoop.example");
    }
  });
});

describe("env.apiUrl, dev build", () => {
  it("keeps working on http://localhost", () => {
    setDev(true);
    process.env.EXPO_PUBLIC_API_URL = "http://localhost:8000";
    expect(env.apiUrl).toBe("http://localhost:8000");
  });

  it("keeps working on a LAN address (a physical device's route to a dev server)", () => {
    setDev(true);
    process.env.EXPO_PUBLIC_API_URL = "http://192.168.1.42:8000";
    expect(env.apiUrl).toBe("http://192.168.1.42:8000");
  });

  it("keeps working on the unset default", () => {
    setDev(true);
    delete process.env.EXPO_PUBLIC_API_URL;
    expect(env.apiUrl).toBe("http://localhost:8000");
  });
});
