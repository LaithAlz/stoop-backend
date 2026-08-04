/**
 * Hook-level test for the M1 senior advisory folded into M2: an undo that
 * 409s with `already_sent` means the reply genuinely went out — the card
 * must flip to "sent" (never flash back to the idle decision card, which
 * would invite a second approve tap on a reply that already left) before
 * the refetch confirms it. Zero network: src/api/drafts.ts is mocked.
 */
import { renderHook, act, waitFor } from "@testing-library/react-native";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "@/api/errors";
import { approveDraft, editAndSendDraft, undoDraftApprove } from "@/api/drafts";
import { useDraftActions } from "../useDraftActions";

jest.mock("@/api/drafts", () => ({
  approveDraft: jest.fn(),
  undoDraftApprove: jest.fn(),
  rejectDraft: jest.fn(),
  editAndSendDraft: jest.fn(),
}));

const mockApprove = approveDraft as jest.Mock;
const mockUndo = undoDraftApprove as jest.Mock;
const mockEditAndSend = editAndSendDraft as jest.Mock;

/** A response envelope whose `.data` throws the instant any property is
 *  read off it — stands in for "the client resolved 2xx, but our own
 *  post-success bookkeeping breaks reading it" (#263's A1 finding; H3
 *  makes the concrete `undo_until`-on-a-null-body case impossible at the
 *  client layer, but the hook's own guard is tested independently here). */
function throwingDataEnvelope(): { data: { undo_until: string }; dateHeader: string } {
  return {
    data: new Proxy(
      {},
      {
        get() {
          throw new Error("boom");
        },
      },
    ) as { undo_until: string },
    dateHeader: new Date().toUTCString(),
  };
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: 0 } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const ctx = { draftId: "draft-1", caseId: "case-1", tenantName: "Maria Gomez" };

describe("useDraftActions — undo 409 already_sent (M1 advisory)", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    // A live, not-yet-expired window so the entry stays "sending" until
    // the undo outcome decides otherwise. #250: approveDraft now resolves
    // the { data, dateHeader } envelope, not the bare body — dateHeader
    // matches "now" so computeUndoExpiresAt's device-clock math lines up
    // with the plain 60s window these tests expect.
    mockApprove.mockResolvedValue({
      data: {
        status: "approved",
        scheduled_send_at: new Date(Date.now() + 60_000).toISOString(),
        undo_until: new Date(Date.now() + 60_000).toISOString(),
      },
      dateHeader: new Date().toUTCString(),
    });
  });

  it("flips the card to 'sent' — not idle — and tells the landlord honestly", async () => {
    mockUndo.mockRejectedValue(
      new ApiError(409, {
        code: "already_sent",
        message: "raw server text",
        request_id: "req_1",
      }),
    );
    const onNotice = jest.fn();
    const onSettled = jest.fn();

    const { result } = renderHook(() => useDraftActions({ onNotice, onSettled }), { wrapper });

    act(() => result.current.approve(ctx));
    await waitFor(() => expect(result.current.entries["draft-1"]?.status).toBe("sending"));

    act(() => result.current.undo(ctx));
    await waitFor(() => expect(result.current.entries["draft-1"]?.status).toBe("sent"));

    // The house line — never the raw server message — and a refetch nudge.
    expect(onNotice).toHaveBeenCalledWith(
      "That reply already went out — there's nothing left to undo.",
    );
    expect(onSettled).toHaveBeenCalled();
  });

  it("any other undo failure still clears the overlay back to the server's truth", async () => {
    mockUndo.mockRejectedValue(
      new ApiError(0, {
        code: "network_error",
        message: "Couldn't reach Stoop. Check your connection and try again.",
        request_id: "req_local",
      }),
    );
    const onNotice = jest.fn();

    const { result } = renderHook(() => useDraftActions({ onNotice, onSettled: jest.fn() }), {
      wrapper,
    });

    act(() => result.current.approve(ctx));
    await waitFor(() => expect(result.current.entries["draft-1"]?.status).toBe("sending"));

    act(() => result.current.undo(ctx));
    await waitFor(() => expect(result.current.entries["draft-1"]).toBeUndefined());

    expect(onNotice).toHaveBeenCalledWith(
      "Couldn't reach Stoop. Check your connection and try again.",
    );
  });

  it("a successful undo returns the card to the idle decision state", async () => {
    mockUndo.mockResolvedValue({ status: "pending" });

    const { result } = renderHook(
      () => useDraftActions({ onNotice: jest.fn(), onSettled: jest.fn() }),
      { wrapper },
    );

    act(() => result.current.approve(ctx));
    await waitFor(() => expect(result.current.entries["draft-1"]?.status).toBe("sending"));

    act(() => result.current.undo(ctx));
    await waitFor(() => expect(result.current.entries["draft-1"]).toBeUndefined());
  });

  /**
   * #250 safety review: the helper's own unit tests (queueEntries.test.ts)
   * pin `computeUndoExpiresAt`, but the bug this issue fixed lived at the
   * CALL SITE — and the reviewer demonstrated that reverting these call
   * sites to `Date.parse(result.data.undo_until)` (the original bug), or
   * passing `null` instead of `result.dateHeader` (anchor never plumbed),
   * leaves the entire suite green. This test is what makes the wiring
   * itself un-revertable: it asserts the landlord-visible symptom — a
   * device clock 10 minutes fast must NOT collapse the window and jump
   * the card straight to "Sent." with no undo offered.
   */
  it("anchors the stored expiry to the response's Date header, not the device clock", async () => {
    const serverNow = new Date(Date.now() - 600_000); // device runs 10 min fast
    // CI-reviewer finding: the fixture window MUST differ from
    // UNDO_WINDOW_FALLBACK_MS (queueEntries.ts, 5_000). At 5_000 the
    // no-anchor fallback value (receivedAtClient + 5_000) lands inside the
    // same assertion range as the correctly-anchored value, so the two
    // mutants this test exists to catch (dateHeader -> null; reverting to
    // Date.parse(result.data.undo_until)) both leave the suite green. 60s
    // is the server's real contract window and is far enough from the
    // fallback that only the correctly-anchored math can satisfy the
    // delta assertion below.
    mockApprove.mockResolvedValue({
      data: {
        status: "approved",
        scheduled_send_at: new Date(serverNow.getTime() + 60_000).toISOString(),
        undo_until: new Date(serverNow.getTime() + 60_000).toISOString(),
      },
      dateHeader: serverNow.toUTCString(),
    });

    const { result } = renderHook(
      () => useDraftActions({ onNotice: jest.fn(), onSettled: jest.fn() }),
      { wrapper },
    );

    const before = Date.now();
    act(() => result.current.approve(ctx));
    await waitFor(() => expect(result.current.entries["draft-1"]?.status).toBe("sending"));

    const entry = result.current.entries["draft-1"];
    if (entry?.status !== "sending") throw new Error("expected a sending entry");
    // Anchored: expiry lands ~60s from RECEIPT on the device clock. Under
    // the old math (server timestamp compared to the device's OWN, 10
    // minutes fast, clock) it would be roughly 9 minutes in the past,
    // i.e. instant expiry, not ~60s out.
    // `approvedAtClient` is the device clock at receipt, so it must sit in
    // the window this test actually spans. Bounds by construction, never
    // flaky, and it stops the delta assertion below from passing on two
    // fields that are wrong together.
    expect(entry.approvedAtClient).toBeGreaterThanOrEqual(before);
    expect(entry.approvedAtClient).toBeLessThanOrEqual(Date.now());
    // R4: measured against `approvedAtClient`, the exact receipt timestamp
    // the anchoring math itself uses, not against a `before` sampled earlier
    // in the test. That removes the dispatch latency from the window
    // entirely, so a loaded CI box stalling between the two cannot flake it.
    const delta = entry.undoExpiresAtClient - entry.approvedAtClient;
    expect(delta).toBeGreaterThan(30_000);
    expect(delta).toBeLessThanOrEqual(61_000);
  });
});

describe("useDraftActions — A1 guard (#263, ported from web's #234 PR 2)", () => {
  beforeEach(() => jest.clearAllMocks());

  it("approve: a throw reading the success payload is never presented as a rejected approve", async () => {
    mockApprove.mockResolvedValue(throwingDataEnvelope());
    const onNotice = jest.fn();
    const onSettled = jest.fn();

    const { result } = renderHook(() => useDraftActions({ onNotice, onSettled }), { wrapper });

    act(() => result.current.approve(ctx));

    await waitFor(() =>
      expect(onNotice).toHaveBeenCalledWith(
        "That went through, but the on-screen countdown didn't update.",
      ),
    );
    expect(onSettled).toHaveBeenCalled();
    // Never the onError/handleError line — that would misreport a
    // successful approve as a failed request.
    expect(onNotice).not.toHaveBeenCalledWith(
      "Something didn't go through. Try again in a moment.",
    );
    // No "cleared" dispatch either — the card must not revert to an
    // approvable-looking idle state on a reply that's already sending.
    expect(result.current.entries["draft-1"]).toBeUndefined();
  });

  it("edit-and-send: same guard, and still closes the editor since the send succeeded", async () => {
    mockEditAndSend.mockResolvedValue(throwingDataEnvelope());
    const onNotice = jest.fn();
    const onSettled = jest.fn();

    const { result } = renderHook(() => useDraftActions({ onNotice, onSettled }), { wrapper });

    act(() => result.current.openEditor(ctx, "original body"));
    expect(result.current.editingContext).not.toBeNull();

    act(() => result.current.submitEdit("edited body"));

    await waitFor(() =>
      expect(onNotice).toHaveBeenCalledWith(
        "That went through, but the on-screen countdown didn't update.",
      ),
    );
    expect(onSettled).toHaveBeenCalled();
    expect(result.current.editingContext).toBeNull();
  });
});
