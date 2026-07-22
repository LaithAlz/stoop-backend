/**
 * docs/03-engineering/api-contracts.md "Devices" section (v1.18 amendment,
 * issue #210 M3) — push-notification device registration. Landlord-scoped,
 * standard error envelope, no rate limiting (matches the backend's own
 * `app/routers/devices.py` docstring: "auth'd, idempotent upsert").
 *
 * Never log a token or device id from this module's call sites (the token
 * is credential-adjacent, CLAUDE.md rule 5-adjacent) — see
 * src/features/push/deviceRegistration.ts, the one caller of both
 * functions below.
 */
import { apiRequest } from "./client";
import type { DeleteDeviceResponse, DeviceResponse, RegisterDeviceInput } from "./types";

/** Upsert on `token` — re-registering the SAME token under the SAME
 *  landlord (e.g. app relaunch, or an unrelated foreground refresh) is a
 *  no-op, never a 409. */
export function registerDevice(input: RegisterDeviceInput): Promise<DeviceResponse> {
  return apiRequest<DeviceResponse>("/v1/devices", { method: "POST", body: input });
}

/** Hard delete by the row's own id (never the raw token — see the doc's
 *  "Contract choice" note). Not idempotent-200 on repeat: a second call
 *  404s `device_not_found`, which every caller here treats as a no-op
 *  success (there's nothing left to unregister either way). */
export function unregisterDevice(id: string): Promise<DeleteDeviceResponse> {
  return apiRequest<DeleteDeviceResponse>(`/v1/devices/${id}`, { method: "DELETE" });
}
