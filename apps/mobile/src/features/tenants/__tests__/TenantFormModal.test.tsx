/**
 * TenantFormModal, issue #292 end-to-end: a legacy, un-normalizable
 * `tenants.phone` (pre-#232/#260 data) must not block an edit to an
 * unrelated field. src/api/tenants.ts is mocked (no network, no
 * @/lib/supabase construction), mirroring src/features/emergency/
 * __tests__/useAcknowledge.test.tsx's fence).
 */
/**
 * CI timeout note. Every `it` here carries an explicit 30s timeout, the
 * same treatment `src/app/__tests__/auth-gate.test.tsx` got in #267 and
 * for the same reason. These mount a real modal over a real
 * QueryClientProvider, and the first test in the file additionally pays
 * module load and first render. In isolation the whole file runs in about
 * 1.2s; on a contended CI runner the first test crossed Jest's 5000ms
 * default and failed while the other three passed, which is the flake
 * profile, not a hang. Nothing here asserts on timing, so the headroom
 * gives a legitimately heavy render room rather than papering over a real
 * wait.
 */
import type { ReactNode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Tenant } from "@/api/types";
import { createTenant, updateTenant } from "@/api/tenants";
import { TenantFormModal } from "../TenantFormModal";

jest.mock("@/api/tenants", () => ({
  createTenant: jest.fn(),
  updateTenant: jest.fn(),
  tenantsQueryKey: (propertyId: string) => ["tenants", propertyId],
}));

const mockCreateTenant = createTenant as jest.Mock;
const mockUpdateTenant = updateTenant as jest.Mock;

// LEGACY_TENANT.phone is un-normalizable on purpose (toE164 -> null): the
// exact shape of a row the server would now 422 on ANY PATCH that resends
// it (#232/#260's canonicalization).
const LEGACY_TENANT: Tenant = {
  id: "tenant-1",
  property_id: "prop-1",
  name: null,
  phone: "call the office",
  unit: null,
  vulnerable_occupant: null,
  notes: null,
  active: true,
  created_at: "2026-01-01T00:00:00Z",
};

const PHONE_FORMAT_ERROR = "Use 10 digits, 11 starting with 1, or + and your country code.";

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: 0 } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return wrapper;
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe("TenantFormModal, #292: a legacy un-normalizable phone stays editable", () => {
  it("toggling vulnerable_occupant and saving sends a PATCH with no phone key", async () => {
    mockUpdateTenant.mockResolvedValue({ ...LEGACY_TENANT, vulnerable_occupant: "infant" });
    const onClose = jest.fn();
    render(
      <TenantFormModal visible propertyId="prop-1" tenant={LEGACY_TENANT} onClose={onClose} />,
      {
        wrapper: makeWrapper(),
      },
    );

    fireEvent.press(screen.getByText("An infant"));
    fireEvent.press(screen.getByTestId("tenant-save"));

    // Never blocked by the stale phone's format, because the field was not touched.
    expect(screen.queryByText(PHONE_FORMAT_ERROR)).toBeNull();

    await waitFor(() => expect(mockUpdateTenant).toHaveBeenCalledTimes(1));
    const [tenantId, body] = mockUpdateTenant.mock.calls[0];
    expect(tenantId).toBe("tenant-1");
    expect(body).not.toHaveProperty("phone");
    expect(body).toEqual({ vulnerable_occupant: "infant" });
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  }, 30000);

  it("changing the phone away from the legacy value re-validates and blocks an un-normalizable one", () => {
    const onClose = jest.fn();
    render(
      <TenantFormModal visible propertyId="prop-1" tenant={LEGACY_TENANT} onClose={onClose} />,
      {
        wrapper: makeWrapper(),
      },
    );

    fireEvent.changeText(screen.getByTestId("tenant-phone"), "still not a number");
    fireEvent.press(screen.getByTestId("tenant-save"));

    expect(screen.getByText(PHONE_FORMAT_ERROR)).toBeTruthy();
    expect(mockUpdateTenant).not.toHaveBeenCalled();
  }, 30000);

  it("changing the phone to a real number sends it, normalized", async () => {
    mockUpdateTenant.mockResolvedValue({ ...LEGACY_TENANT, phone: "+14165550199" });
    const onClose = jest.fn();
    render(
      <TenantFormModal visible propertyId="prop-1" tenant={LEGACY_TENANT} onClose={onClose} />,
      {
        wrapper: makeWrapper(),
      },
    );

    fireEvent.changeText(screen.getByTestId("tenant-phone"), "(416) 555-0199");
    fireEvent.press(screen.getByTestId("tenant-save"));

    await waitFor(() => expect(mockUpdateTenant).toHaveBeenCalledTimes(1));
    const [, body] = mockUpdateTenant.mock.calls[0];
    expect(body).toEqual({ phone: "+14165550199" });
  }, 30000);
});

// Adversarial safety review, 2026-08-04, item 1 (FIX 1, HIGH): the exact
// legacy row most likely to survive migration 0017 untouched (schema-v1.md
// v1.21 point 5) is one with surrounding whitespace on top of the
// unnormalizable text (a CSV import, a paste from a spreadsheet, a
// trailing newline). Before the fix this whole suite passed against
// LEGACY_TENANT's exact phone but failed for every one of these.
describe("TenantFormModal, item 1: a legacy phone with surrounding whitespace stays editable", () => {
  it("a trailing space on the stored phone does not block an unrelated edit", async () => {
    const tenantWithTrailingSpace: Tenant = { ...LEGACY_TENANT, phone: "call the office " };
    mockUpdateTenant.mockResolvedValue({
      ...tenantWithTrailingSpace,
      vulnerable_occupant: "infant",
    });
    const onClose = jest.fn();
    render(
      <TenantFormModal
        visible
        propertyId="prop-1"
        tenant={tenantWithTrailingSpace}
        onClose={onClose}
      />,
      { wrapper: makeWrapper() },
    );

    fireEvent.press(screen.getByText("An infant"));
    fireEvent.press(screen.getByTestId("tenant-save"));

    expect(screen.queryByText(PHONE_FORMAT_ERROR)).toBeNull();
    await waitFor(() => expect(mockUpdateTenant).toHaveBeenCalledTimes(1));
    const [, body] = mockUpdateTenant.mock.calls[0];
    expect(body).not.toHaveProperty("phone");
    expect(body).toEqual({ vulnerable_occupant: "infant" });
  }, 30000);

  it("a leading space on the stored phone does not block an unrelated edit", async () => {
    const tenantWithLeadingSpace: Tenant = { ...LEGACY_TENANT, phone: " call the office" };
    mockUpdateTenant.mockResolvedValue({
      ...tenantWithLeadingSpace,
      vulnerable_occupant: "elderly",
    });
    const onClose = jest.fn();
    render(
      <TenantFormModal
        visible
        propertyId="prop-1"
        tenant={tenantWithLeadingSpace}
        onClose={onClose}
      />,
      { wrapper: makeWrapper() },
    );

    fireEvent.press(screen.getByText("An elderly person"));
    fireEvent.press(screen.getByTestId("tenant-save"));

    expect(screen.queryByText(PHONE_FORMAT_ERROR)).toBeNull();
    await waitFor(() => expect(mockUpdateTenant).toHaveBeenCalledTimes(1));
    const [, body] = mockUpdateTenant.mock.calls[0];
    expect(body).not.toHaveProperty("phone");
  }, 30000);

  it("a trailing newline on the stored phone does not block an unrelated edit", async () => {
    const tenantWithTrailingNewline: Tenant = {
      ...LEGACY_TENANT,
      phone: "416-555-0134 x22\n",
    };
    mockUpdateTenant.mockResolvedValue({
      ...tenantWithTrailingNewline,
      vulnerable_occupant: "medical_device",
    });
    const onClose = jest.fn();
    render(
      <TenantFormModal
        visible
        propertyId="prop-1"
        tenant={tenantWithTrailingNewline}
        onClose={onClose}
      />,
      { wrapper: makeWrapper() },
    );

    fireEvent.press(screen.getByText("On powered medical equipment"));
    fireEvent.press(screen.getByTestId("tenant-save"));

    expect(screen.queryByText(PHONE_FORMAT_ERROR)).toBeNull();
    await waitFor(() => expect(mockUpdateTenant).toHaveBeenCalledTimes(1));
    const [, body] = mockUpdateTenant.mock.calls[0];
    expect(body).not.toHaveProperty("phone");
  }, 30000);
});

// Item 3 (FIX 3, MEDIUM): the one field-level signal for a tenant whose
// stored, unchanged phone is un-normalizable, non-blocking on purpose.
describe("TenantFormModal, item 3: unreachable-number warning", () => {
  it("shows a non-blocking warning as soon as the modal opens, before any submit attempt", () => {
    const onClose = jest.fn();
    render(
      <TenantFormModal visible propertyId="prop-1" tenant={LEGACY_TENANT} onClose={onClose} />,
      { wrapper: makeWrapper() },
    );

    expect(screen.getByTestId("tenant-phone-warning")).toBeTruthy();
    // Never the blocking format error, and never the neutral helper.
    expect(screen.queryByText(PHONE_FORMAT_ERROR)).toBeNull();
  }, 30000);

  it("does not block Save while the warning is showing", async () => {
    mockUpdateTenant.mockResolvedValue({ ...LEGACY_TENANT, vulnerable_occupant: "infant" });
    const onClose = jest.fn();
    render(
      <TenantFormModal visible propertyId="prop-1" tenant={LEGACY_TENANT} onClose={onClose} />,
      { wrapper: makeWrapper() },
    );

    expect(screen.getByTestId("tenant-phone-warning")).toBeTruthy();
    fireEvent.press(screen.getByText("An infant"));
    fireEvent.press(screen.getByTestId("tenant-save"));

    await waitFor(() => expect(mockUpdateTenant).toHaveBeenCalledTimes(1));
  }, 30000);

  it("clears once the landlord actually edits the phone field", () => {
    const onClose = jest.fn();
    render(
      <TenantFormModal visible propertyId="prop-1" tenant={LEGACY_TENANT} onClose={onClose} />,
      { wrapper: makeWrapper() },
    );

    expect(screen.getByTestId("tenant-phone-warning")).toBeTruthy();
    fireEvent.changeText(screen.getByTestId("tenant-phone"), "(416) 555-0199");

    expect(screen.queryByTestId("tenant-phone-warning")).toBeNull();
  }, 30000);

  it("never shows for a tenant with a normalized, dialable phone", () => {
    const onClose = jest.fn();
    const normalTenant: Tenant = { ...LEGACY_TENANT, phone: "+14165550134" };
    render(
      <TenantFormModal visible propertyId="prop-1" tenant={normalTenant} onClose={onClose} />,
      { wrapper: makeWrapper() },
    );

    expect(screen.queryByTestId("tenant-phone-warning")).toBeNull();
  }, 30000);
});

// Item 4 (FIX 4, MEDIUM): a pre-#260 blank stored phone must not read as
// "unchanged": the required-field error has to keep showing until the
// landlord actually fixes it, exactly as it would for a brand-new tenant.
describe("TenantFormModal, item 4: a blank stored phone still requires a fix", () => {
  it("blocks Save with the required-field error, unchanged", () => {
    const blankPhoneTenant: Tenant = { ...LEGACY_TENANT, phone: "" };
    const onClose = jest.fn();
    render(
      <TenantFormModal visible propertyId="prop-1" tenant={blankPhoneTenant} onClose={onClose} />,
      { wrapper: makeWrapper() },
    );

    fireEvent.press(screen.getByText("An infant"));
    fireEvent.press(screen.getByTestId("tenant-save"));

    expect(screen.getByText("Add a phone number.")).toBeTruthy();
    expect(mockUpdateTenant).not.toHaveBeenCalled();
  }, 30000);

  it("saves once the landlord supplies a real phone", async () => {
    const blankPhoneTenant: Tenant = { ...LEGACY_TENANT, phone: "" };
    mockUpdateTenant.mockResolvedValue({ ...blankPhoneTenant, phone: "+14165550134" });
    const onClose = jest.fn();
    render(
      <TenantFormModal visible propertyId="prop-1" tenant={blankPhoneTenant} onClose={onClose} />,
      { wrapper: makeWrapper() },
    );

    fireEvent.changeText(screen.getByTestId("tenant-phone"), "(416) 555-0134");
    fireEvent.press(screen.getByTestId("tenant-save"));

    await waitFor(() => expect(mockUpdateTenant).toHaveBeenCalledTimes(1));
    const [, body] = mockUpdateTenant.mock.calls[0];
    expect(body).toEqual({ phone: "+14165550134" });
  }, 30000);
});

describe("TenantFormModal, create mode is unaffected", () => {
  it("still requires and sends a validated phone for a brand-new tenant", async () => {
    mockCreateTenant.mockResolvedValue({ ...LEGACY_TENANT, id: "tenant-2" });
    const onClose = jest.fn();
    render(<TenantFormModal visible propertyId="prop-1" tenant={null} onClose={onClose} />, {
      wrapper: makeWrapper(),
    });

    fireEvent.press(screen.getByTestId("tenant-save"));
    expect(screen.getByText("Add a phone number.")).toBeTruthy();
    expect(mockCreateTenant).not.toHaveBeenCalled();

    fireEvent.changeText(screen.getByTestId("tenant-phone"), "(416) 555-0134");
    fireEvent.press(screen.getByTestId("tenant-save"));

    await waitFor(() => expect(mockCreateTenant).toHaveBeenCalledTimes(1));
    const [propertyId, body] = mockCreateTenant.mock.calls[0];
    expect(propertyId).toBe("prop-1");
    expect(body).toEqual({ phone: "+14165550134" });
  }, 30000);
});
