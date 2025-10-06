"use client";

import { useEffect, useMemo, useState } from "react";

type ManifestDocument = {
  id: string;
  label: string;
  filename_pattern?: string;
  required: boolean;
  content_types?: string[];
  max_mb: number;
  max_pages: number;
  required_sections?: string[];
  notes?: string;
};

type ManifestResponse = {
  opportunity_id: string;
  title: string;
  documents: ManifestDocument[];
};

type StatusResponse = {
  submission_id: string;
  opportunity_id?: string;
  overall: string;
  files: Array<{
    requirement_id: string;
    filename?: string;
    status: string;
    messages: string[];
  }>;
  updated_at?: string;
};

type UploadDescriptor = {
  url: string;
  method: string;
  headers: Record<string, string>;
};

type UploadUrlResponse = {
  submission_id: string;
  requirement_id: string;
  key: string;
  upload: UploadDescriptor;
};

type ManifestIndex = Record<string, { title: string }>;

const API_BASE = process.env.NEXT_PUBLIC_DOC_CHECKER_API ?? "http://localhost:8000";
const STORAGE_KEY = "grant-doc-checker-submission";

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`Request failed: ${response.status} ${detail}`);
  }
  return (await response.json()) as T;
}

function useLocalSubmissionId(): [string | null, (value: string | null) => void] {
  const [submissionId, setSubmissionId] = useState<string | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored) setSubmissionId(stored);
  }, []);

  const update = (value: string | null) => {
    if (typeof window !== "undefined") {
      if (value) {
        window.localStorage.setItem(STORAGE_KEY, value);
      } else {
        window.localStorage.removeItem(STORAGE_KEY);
      }
    }
    setSubmissionId(value);
  };

  return [submissionId, update];
}

async function uploadWithProgress(
  descriptor: UploadDescriptor,
  file: File,
  onProgress: (value: number) => void
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(descriptor.method, descriptor.url, true);

    Object.entries(descriptor.headers || {}).forEach(([key, value]) => {
      xhr.setRequestHeader(key, value);
    });

    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      const progress = Math.round((event.loaded / event.total) * 100);
      onProgress(progress);
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(100);
        resolve();
      } else {
        reject(new Error(`Upload failed with status ${xhr.status}`));
      }
    };

    xhr.onerror = () => reject(new Error("Upload error"));
    xhr.send(file);
  });
}

export default function Page() {
  const [manifestIndex, setManifestIndex] = useState<ManifestIndex>({});
  const [opportunityId, setOpportunityId] = useState<string>("opp-001");
  const [manifest, setManifest] = useState<ManifestResponse | null>(null);
  const [submissionId, setSubmissionId] = useLocalSubmissionId();
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState<Record<string, number>>({});
  const [error, setError] = useState<string | null>(null);

  const documents = manifest?.documents ?? [];
  const statusMap = useMemo(() => {
    const map: Record<string, StatusResponse["files"][number]> = {};
    for (const entry of status?.files ?? []) {
      map[entry.requirement_id] = entry;
    }
    return map;
  }, [status]);

  useEffect(() => {
    jsonFetch<{ opportunities: ManifestIndex }>(`${API_BASE}/manifest/index`)
      .then((data) => setManifestIndex(data.opportunities))
      .catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    setError(null);
    jsonFetch<ManifestResponse>(`${API_BASE}/manifest?opportunity_id=${opportunityId}`)
      .then(setManifest)
      .catch((err) => setError(err.message));
  }, [opportunityId]);

  useEffect(() => {
    if (!submissionId) return;
    refreshStatus(submissionId);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [submissionId]);

  const refreshStatus = async (id: string) => {
    try {
      const data = await jsonFetch<StatusResponse>(`${API_BASE}/status/${id}`);
      setStatus(data);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const handleStart = async (): Promise<string | null> => {
    try {
      setLoading(true);
      setStatus(null);
      const response = await jsonFetch<{
        submission_id: string;
        opportunity_id?: string;
      }>(`${API_BASE}/start-submission`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ opportunity_id: opportunityId }),
      });
      setSubmissionId(response.submission_id);
      await refreshStatus(response.submission_id);
      return response.submission_id;
    } catch (err) {
      setError((err as Error).message);
      return null;
    } finally {
      setLoading(false);
    }
  };

  const handleUpload = async (requirementId: string, file: File) => {
    try {
      setError(null);
      let activeSubmission = submissionId;
      if (!activeSubmission) {
        activeSubmission = await handleStart();
      }
      if (!activeSubmission) {
        throw new Error("Unable to initialise submission");
      }
      const payload = {
        filename: file.name,
        contentType: file.type || "application/octet-stream",
        submission_id: activeSubmission,
        opportunity_id: opportunityId,
        requirement_id: requirementId,
      };
      const descriptor = await jsonFetch<UploadUrlResponse>(`${API_BASE}/upload-url`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setSubmissionId(descriptor.submission_id);
      setUploading((current) => ({ ...current, [requirementId]: 1 }));
      await uploadWithProgress(descriptor.upload, file, (progress) => {
        setUploading((current) => ({ ...current, [requirementId]: progress }));
      });
      await refreshStatus(descriptor.submission_id);
      setTimeout(() => {
        setUploading((current) => {
          const next = { ...current };
          delete next[requirementId];
          return next;
        });
      }, 1500);
    } catch (err) {
      setError((err as Error).message);
      setUploading((current) => {
        const next = { ...current };
        delete next[requirementId];
        return next;
      });
    }
  };

  const opportunityOptions = Object.entries(manifestIndex);

  return (
    <main style={{ padding: "2rem", color: "#e2e8f0" }}>
      <section style={{ maxWidth: "960px", margin: "0 auto" }}>
        <header style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <h1 style={{ margin: 0 }}>Grants.gov Document Checker</h1>
          <p style={{ margin: 0, color: "#94a3b8" }}>
            Upload required files for an opportunity. The system validates filenames, types, sizes, and required
            sections. Files expire automatically after 48 hours.
          </p>
          <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap" }}>
            <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
              <span>Opportunity</span>
              <select
                value={opportunityId}
                onChange={(event) => setOpportunityId(event.target.value)}
                style={{ padding: "0.5rem", minWidth: "240px" }}
              >
                {opportunityOptions.map(([id, item]) => (
                  <option key={id} value={id}>
                    {item.title || id}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              onClick={handleStart}
              disabled={loading}
              style={{ alignSelf: "flex-end", padding: "0.6rem 1.2rem" }}
            >
              {submissionId ? "Reset Submission" : "Start Submission"}
            </button>
            <button
              type="button"
              onClick={() => submissionId && refreshStatus(submissionId)}
              disabled={!submissionId}
              style={{ alignSelf: "flex-end", padding: "0.6rem 1.2rem" }}
            >
              Run Checks
            </button>
          </div>
          {submissionId && (
            <small style={{ color: "#38bdf8" }}>Submission ID: {submissionId}</small>
          )}
          {error && <div style={{ color: "#f87171" }}>{error}</div>}
        </header>

        <section style={{ marginTop: "2rem", display: "grid", gap: "1rem" }}>
          {documents.map((doc) => {
            const statusEntry = statusMap[doc.id];
            const progress = uploading[doc.id];
            return (
              <article
                key={doc.id}
                style={{
                  padding: "1rem",
                  borderRadius: "12px",
                  border: "1px solid rgba(148,163,184,0.25)",
                  background: "rgba(15,23,42,0.85)",
                  display: "flex",
                  flexDirection: "column",
                  gap: "0.75rem",
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", gap: "1rem", flexWrap: "wrap" }}>
                  <div>
                    <h2 style={{ margin: "0 0 0.25rem 0" }}>{doc.label}</h2>
                    <small style={{ color: "#94a3b8" }}>
                      {doc.required ? "Required" : "Optional"} • Max {doc.max_mb}MB • Up to {doc.max_pages} pages
                    </small>
                  </div>
                  <StatusBadge status={statusEntry?.status ?? "pending"} />
                </div>

                <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap", alignItems: "center" }}>
                  <label style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                    <input
                      type="file"
                      accept={doc.content_types?.join(",")}
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (!file) return;
                        handleUpload(doc.id, file);
                        event.target.value = "";
                      }}
                      style={{ display: "none" }}
                    />
                    <span
                      style={{
                        padding: "0.5rem 1rem",
                        borderRadius: "8px",
                        border: "1px solid rgba(148,163,184,0.45)",
                        cursor: "pointer",
                      }}
                    >
                      Upload File
                    </span>
                  </label>
                  {progress !== undefined && (
                    <span style={{ color: "#38bdf8" }}>Uploading… {progress}%</span>
                  )}
                  {statusEntry?.filename && <span>{statusEntry.filename}</span>}
                </div>

                <details>
                  <summary style={{ cursor: "pointer" }}>Validation rules</summary>
                  <ul>
                    {doc.filename_pattern && <li>Filename must match regex: {doc.filename_pattern}</li>}
                    {doc.content_types && doc.content_types.length > 0 && (
                      <li>Allowed types: {doc.content_types.join(", ")}</li>
                    )}
                    {doc.required_sections && doc.required_sections.length > 0 && (
                      <li>Required sections: {doc.required_sections.join(", ")}</li>
                    )}
                    {doc.notes && <li>{doc.notes}</li>}
                  </ul>
                </details>

                {statusEntry?.messages?.length ? (
                  <div>
                    <strong>Messages:</strong>
                    <ul>
                      {statusEntry.messages.map((message) => (
                        <li key={message}>{message}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </article>
            );
          })}
        </section>
      </section>
    </main>
  );
}

function StatusBadge({ status }: { status: string }) {
  const palette: Record<string, { label: string; color: string; background: string }> = {
    valid: { label: "Ready", color: "#22c55e", background: "rgba(34,197,94,0.1)" },
    passed: { label: "Passed", color: "#22c55e", background: "rgba(34,197,94,0.1)" },
    pending: { label: "Pending", color: "#fbbf24", background: "rgba(251,191,36,0.1)" },
    needs_review: { label: "Needs review", color: "#f87171", background: "rgba(248,113,113,0.1)" },
    invalid: { label: "Invalid", color: "#f87171", background: "rgba(248,113,113,0.1)" },
    error: { label: "Error", color: "#f87171", background: "rgba(248,113,113,0.1)" },
  };
  const resolved = palette[status] ?? palette.pending;
  return (
    <span
      style={{
        padding: "0.35rem 0.75rem",
        borderRadius: "999px",
        fontSize: "0.85rem",
        color: resolved.color,
        background: resolved.background,
      }}
    >
      {resolved.label}
    </span>
  );
}
