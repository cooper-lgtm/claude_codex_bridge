use std::env;
use std::path::PathBuf;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Args {
    pub ccbd_socket: PathBuf,
    pub project_root: PathBuf,
    pub pane_window: String,
}

impl Args {
    pub fn parse_from<I, S>(items: I) -> Result<Self, String>
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        let mut ccbd_socket: Option<PathBuf> = None;
        let mut project_root: Option<PathBuf> = None;
        let mut pane_window: Option<String> = None;
        let mut iter = items.into_iter().map(Into::into);

        while let Some(item) = iter.next() {
            match item.as_str() {
                "--ccbd-socket" => {
                    ccbd_socket = Some(PathBuf::from(next_value(&mut iter, "--ccbd-socket")?));
                }
                "--project-root" => {
                    project_root = Some(PathBuf::from(next_value(&mut iter, "--project-root")?));
                }
                "--pane-window" => {
                    pane_window = Some(non_empty(
                        next_value(&mut iter, "--pane-window")?,
                        "--pane-window",
                    )?);
                }
                "-h" | "--help" => return Err(usage()),
                other => return Err(format!("unknown argument: {other}\n{}", usage())),
            }
        }

        Ok(Self {
            ccbd_socket: ccbd_socket.ok_or_else(|| missing("--ccbd-socket"))?,
            project_root: project_root.ok_or_else(|| missing("--project-root"))?,
            pane_window: pane_window.ok_or_else(|| missing("--pane-window"))?,
        })
    }

    pub fn parse_env() -> Result<Self, String> {
        Self::parse_from(env::args().skip(1))
    }
}

fn next_value(iter: &mut impl Iterator<Item = String>, flag: &str) -> Result<String, String> {
    iter.next()
        .ok_or_else(|| format!("missing value for {flag}"))
}

fn non_empty(value: String, flag: &str) -> Result<String, String> {
    let text = value.trim().to_string();
    if text.is_empty() {
        Err(format!("empty value for {flag}"))
    } else {
        Ok(text)
    }
}

fn missing(flag: &str) -> String {
    format!("missing required argument {flag}\n{}", usage())
}

pub fn usage() -> String {
    "usage: ccb-agent-sidebar --ccbd-socket <path> --project-root <path> --pane-window <name>"
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_required_arguments() {
        let args = Args::parse_from([
            "--ccbd-socket",
            "/tmp/ccbd.sock",
            "--project-root",
            "/repo",
            "--pane-window",
            "main",
        ])
        .unwrap();

        assert_eq!(args.ccbd_socket, PathBuf::from("/tmp/ccbd.sock"));
        assert_eq!(args.project_root, PathBuf::from("/repo"));
        assert_eq!(args.pane_window, "main");
    }

    #[test]
    fn rejects_missing_socket() {
        let err =
            Args::parse_from(["--project-root", "/repo", "--pane-window", "main"]).unwrap_err();
        assert!(err.contains("--ccbd-socket"));
    }
}
