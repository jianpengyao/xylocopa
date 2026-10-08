import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { createProject } from "../lib/api";
import useDraft from "../hooks/useDraft";
import PageHeader from "../components/PageHeader";
import { useToast } from "../contexts/ToastContext";

// New Project is the only flow on this page. Agent and Task creation
// previously had their own cards here; they were removed in favor of
// long-press-+ → /new (project) for project-only entry, with agents/tasks
// created from project-context paths.

export default function NewPage({ theme, onToggleTheme }) {
  const navigate = useNavigate();
  const toast = useToast();
  const showToast = (message, type = "success") =>
    type === "error" ? toast.error(message) : toast.success(message);

  return (
    <div className="h-full flex flex-col">
      <PageHeader title="Create Project" theme={theme} onToggleTheme={onToggleTheme} />
      <div className="flex-1 overflow-y-auto overflow-x-hidden">
        <div className="pb-24 p-4 max-w-xl mx-auto w-full">
          <NewProjectForm showToast={showToast} navigate={navigate} />
        </div>
      </div>
    </div>
  );
}

function NewProjectForm({ showToast, navigate }) {
  const [name, setName, clearName] = useDraft("create-project:name", "");
  const [gitUrl, setGitUrl, clearGitUrl] = useDraft("create-project:gitUrl", "");
  const [description, setDescription, clearDesc] = useDraft("create-project:description", "");
  const [submitting, setSubmitting] = useState(false);
  const clearAllDrafts = () => { clearName(); clearGitUrl(); clearDesc(); };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!name.trim()) { showToast("Enter a project name.", "error"); return; }
    setSubmitting(true);
    try {
      const body = { name: name.trim() };
      if (gitUrl.trim()) body.git_url = gitUrl.trim();
      if (description.trim()) body.description = description.trim();
      await createProject(body);
      clearAllDrafts();
      navigate("/projects");
    } catch (err) {
      showToast("Failed: " + err.message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} autoComplete="off" className="space-y-5">
      <div className="rounded-xl bg-surface shadow-card p-4 space-y-4">
        <div>
          <label className="block text-sm font-medium text-label mb-2">
            Name <span className="text-danger">*</span>
          </label>
          <input
            type="text"
            name="project-name"
            autoComplete="off"
            value={name}
            onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9._-]/g, ""))}
            placeholder="my-project"
            className="w-full min-h-[44px] rounded-lg bg-input border border-edge px-3 py-2 text-heading placeholder-hint font-mono focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent transition-colors"
          />
          <p className="text-xs text-dim mt-1">Lowercase letters, numbers, hyphens, underscores, dots</p>
        </div>

        <div>
          <label className="block text-sm font-medium text-label mb-2">Git URL</label>
          <input
            type="text"
            name="git-url"
            autoComplete="off"
            value={gitUrl}
            onChange={(e) => setGitUrl(e.target.value)}
            placeholder="https://github.com/org/repo.git"
            className="w-full min-h-[44px] rounded-lg bg-input border border-edge px-3 py-2 text-heading placeholder-hint focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent transition-colors"
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-label mb-2">Description</label>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What does this project do?"
            rows={2}
            className="w-full rounded-lg bg-input border border-edge px-3 py-2 text-heading placeholder-hint resize-none focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent transition-colors"
          />
        </div>
      </div>

      <button
        type="submit"
        disabled={submitting || !name.trim()}
        className={`w-full min-h-[48px] rounded-lg text-base font-semibold transition-colors ${
          submitting || !name.trim()
            ? "bg-elevated text-dim cursor-not-allowed"
            : "bg-accent hover:opacity-90 text-accent-ink shadow-md"
        }`}
      >
        {submitting ? "Creating Project..." : "Create Project"}
      </button>
    </form>
  );
}
