import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal()),
  filesExistsBatch: vi.fn(),
}));

import { filesExistsBatch } from "./api";
import { useBatchExists, useFileExists } from "./mediaState";

const STAT = { exists: true, size: 10, mtime: 1700000000.5 };

describe("media existence stats across a media-token change", () => {
  beforeEach(() => {
    filesExistsBatch.mockReset();
    filesExistsBatch.mockImplementation(async (items) => ({ results: items.map(() => STAT) }));
  });

  it("useBatchExists keys by the token-free URL and does not re-probe on a new token", async () => {
    const { result, rerender } = renderHook(({ urls }) => useBatchExists(urls), {
      initialProps: { urls: ["/api/files/p/a.png?token=old", "/api/uploads/u.png?token=old"] },
    });
    await waitFor(() => expect(result.current.statMap["/api/files/p/a.png"]).toEqual(STAT));
    expect(result.current.statMap["/api/uploads/u.png"]).toEqual(STAT);
    expect(filesExistsBatch).toHaveBeenCalledTimes(1);
    expect(filesExistsBatch.mock.calls[0][0]).toEqual([{ project: "p", path: "a.png" }, { upload: "u.png" }]);

    rerender({ urls: ["/api/files/p/a.png?token=new", "/api/uploads/u.png?token=new"] });
    // Same keys: the stat is there on this very render, no null flash.
    expect(result.current.statMap["/api/files/p/a.png"]).toEqual(STAT);
    await new Promise((r) => setTimeout(r, 0));
    expect(filesExistsBatch).toHaveBeenCalledTimes(1);
  });

  it("useFileExists returns the same stat for the old and the new token URL", async () => {
    const { result, rerender } = renderHook(({ url }) => useFileExists(url), {
      initialProps: { url: "/api/files/p/v.mp4?token=old" },
    });
    await waitFor(() => expect(result.current.stat).toEqual(STAT));
    rerender({ url: "/api/files/p/v.mp4?token=new" });
    expect(result.current.stat).toEqual(STAT);
    expect(filesExistsBatch).toHaveBeenCalledTimes(1);
  });
});
