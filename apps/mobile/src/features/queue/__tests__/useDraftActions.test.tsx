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
import { approveDraft, undoDraftApprove } from "@/api/drafts";
import { useDraftActions } from "../useDraftActions";

jest.mock("@/api/drafts", () => ({
  approveDraft: jest.fn(),
  undoDraftApprove: jest.fn(),
  rejectDraft: jest.fn(),
  editAndSendDraft: jest.fn(),
}));

const mockApprove = approveDraft as jest.Mock;
const mockUndo = undoDraftApprove as jest.Mock;

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
    // the undo outcome decides otherwise.
    mockApprove.mockResolvedValue({
      status: "approved",
      scheduled_send_at: new Date(Date.now() + 60_000).toISOString(),
      undo_until: new Date(Date.now() + 60_000).toISOString(),
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
});
