import { execSync } from "node:child_process";

/**
 * Short commit SHA for this build. Cloudflare Pages provides the SHA as an env
 * var (the build runs from a shallow checkout without a usable git binary), so
 * prefer that and fall back to asking git locally.
 */
function resolveCommit(): string | null {
  const fromEnv =
    process.env.CF_PAGES_COMMIT_SHA ??
    process.env.GITHUB_SHA ??
    process.env.COMMIT_SHA;
  if (fromEnv) return fromEnv.slice(0, 7);

  try {
    return execSync("git rev-parse --short=7 HEAD", {
      stdio: ["ignore", "pipe", "ignore"],
    })
      .toString()
      .trim();
  } catch {
    return null;
  }
}

export const commit = resolveCommit();
export const commitUrl = commit
  ? `https://github.com/ssamjh/JoinMyMusic.com/commit/${commit}`
  : null;
