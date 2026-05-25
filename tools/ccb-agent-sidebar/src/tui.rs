use std::collections::HashSet;
use std::env;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use crossterm::event::{self, Event, KeyCode};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::prelude::{Color, Frame, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, List, ListItem, Paragraph};

use crate::args::Args;
use crate::client::CcbdClient;
use crate::model::{
    AgentView, CommsItem, ProjectView, ProjectViewResponse, RowTarget, WindowView, row_targets,
};
use crate::status::{activity_color, activity_symbol};

const PROJECT_VIEW_REFRESH_MIN_MS: u64 = 100;
const PROJECT_VIEW_REFRESH_MAX_MS: u64 = 5000;
const PROJECT_VIEW_REFRESH_DEFAULT_MS: u64 = 1000;
const COMMS_ACTION_RETRY_COLS: std::ops::RangeInclusive<u16> = 0..=1;
const COMMS_ACTION_CANCEL_COLS: std::ops::RangeInclusive<u16> = 3..=4;
const COMMS_ACTION_CLEAR_COLS: std::ops::RangeInclusive<u16> = 6..=7;
const TREE_CONTROL_CONTENT_WIDTH: u16 = 3;
const TREE_RESTART_SYMBOL: &str = "↻";
const TREE_KILL_SYMBOL: &str = "×";

pub fn run(args: Args) -> io::Result<()> {
    let action = run_tui(&args)?;
    match action {
        ExitAction::SidebarOnly => {}
        ExitAction::KillProject => run_ccb_kill(&args.project_root)?,
    }
    Ok(())
}

fn run_tui(args: &Args) -> io::Result<ExitAction> {
    let mut stdout = io::stdout();
    enable_raw_mode()?;
    if let Err(err) = execute!(stdout, EnterAlternateScreen) {
        let _ = disable_raw_mode();
        return Err(err);
    }
    let _session = TuiSession;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;
    let client = CcbdClient::new(args.ccbd_socket.clone());
    let mut app = SidebarApp::new(args.pane_window.clone());

    loop {
        if app.needs_refresh() {
            match client.project_view() {
                Ok(response) => app.apply_response(response),
                Err(err) => app.set_error(err),
            }
        }

        terminal.draw(|frame| draw(frame, &app))?;

        if event::poll(Duration::from_millis(250))? {
            match event::read()? {
                Event::Key(key) => match key.code {
                    KeyCode::Char('q') | KeyCode::Esc => return Ok(ExitAction::SidebarOnly),
                    KeyCode::Char('Q') => return Ok(ExitAction::KillProject),
                    KeyCode::Char('j') | KeyCode::Down => app.move_selection(1),
                    KeyCode::Char('k') | KeyCode::Up => app.move_selection(-1),
                    KeyCode::Char('r') => app.force_refresh(),
                    KeyCode::Char('R') => app.restart_project_panes(&client),
                    KeyCode::Enter => app.focus_selected_target(&client),
                    KeyCode::Tab => app.focus_pane_window(&client),
                    _ => {}
                },
                _ => {}
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ExitAction {
    SidebarOnly,
    KillProject,
}

#[cfg(test)]
fn run_ccbd_restart_panes(socket_path: &Path) -> io::Result<()> {
    CcbdClient::new(socket_path.to_path_buf())
        .restart_panes()
        .map_err(io::Error::other)
}

fn run_ccb_kill(project_root: &Path) -> io::Result<()> {
    run_ccb_kill_with_program(ccb_program(), project_root)
}

fn run_ccb_kill_with_program(program: PathBuf, project_root: &Path) -> io::Result<()> {
    let status = Command::new(program)
        .arg("kill")
        .current_dir(project_root)
        .status()?;
    if status.success() {
        return Ok(());
    }
    Err(io::Error::other(format!(
        "ccb kill failed with status {status}"
    )))
}

fn ccb_program() -> PathBuf {
    env::current_exe()
        .ok()
        .and_then(|path| ccb_sibling_for_sidebar(&path))
        .unwrap_or_else(|| PathBuf::from("ccb"))
}

fn ccb_sibling_for_sidebar(sidebar_exe: &Path) -> Option<PathBuf> {
    let candidate = sidebar_exe.parent()?.join("ccb");
    if candidate.exists() {
        Some(candidate)
    } else {
        None
    }
}

struct TuiSession;

impl Drop for TuiSession {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let mut stdout = io::stdout();
        let _ = execute!(stdout, LeaveAlternateScreen);
    }
}

#[derive(Debug, Clone)]
pub struct SidebarApp {
    pane_window: String,
    response: Option<ProjectViewResponse>,
    last_error: Option<String>,
    failure_count: u32,
    selected: usize,
    selected_comms: Option<usize>,
    hidden_comms: HashSet<String>,
    selection_follows_focus: bool,
    refresh_after: Instant,
}

impl SidebarApp {
    pub fn new(pane_window: String) -> Self {
        Self {
            pane_window,
            response: None,
            last_error: None,
            failure_count: 0,
            selected: 0,
            selected_comms: None,
            hidden_comms: HashSet::new(),
            selection_follows_focus: true,
            refresh_after: Instant::now(),
        }
    }

    pub fn apply_response(&mut self, response: ProjectViewResponse) {
        self.response = Some(response);
        self.last_error = None;
        self.failure_count = 0;
        if self.selection_follows_focus {
            self.select_active_target();
        } else {
            self.clamp_selection();
        }
        self.refresh_after = Instant::now() + self.refresh_interval();
    }

    pub fn set_error(&mut self, error: String) {
        self.last_error = Some(error);
        self.failure_count = self.failure_count.saturating_add(1);
        self.refresh_after = Instant::now() + refresh_backoff_for_failures(self.failure_count);
    }

    pub fn force_refresh(&mut self) {
        self.refresh_after = Instant::now();
    }

    pub fn needs_refresh(&self) -> bool {
        Instant::now() >= self.refresh_after
    }

    pub fn move_selection(&mut self, delta: isize) {
        let count = self.targets().len();
        if count == 0 {
            self.selected = 0;
            return;
        }
        let next = (self.selected as isize + delta).clamp(0, (count - 1) as isize);
        self.selected = next as usize;
        self.selection_follows_focus = false;
    }

    pub fn selected_target(&self) -> Option<RowTarget> {
        self.targets().get(self.selected).cloned()
    }

    pub fn namespace_epoch(&self) -> Option<u64> {
        self.view().and_then(|view| view.namespace.epoch)
    }

    pub fn focus_selected_target(&mut self, client: &CcbdClient) {
        if let Some(target) = self.selected_target() {
            self.selection_follows_focus = true;
            self.focus_target(client, target);
        }
    }

    pub fn focus_pane_window(&mut self, client: &CcbdClient) {
        self.selection_follows_focus = true;
        self.focus_target(client, RowTarget::Window(self.pane_window.clone()));
    }

    pub fn focus_target_at(&mut self, column: u16, row: u16, area: Rect, client: &CcbdClient) {
        let Some(index) = self.target_index_at(column, row, area) else {
            return;
        };
        self.selected = index;
        self.selection_follows_focus = true;
        self.focus_selected_target(client);
    }

    pub fn handle_mouse_down(&mut self, column: u16, row: u16, area: Rect, client: &CcbdClient) {
        if self.handle_comms_mouse_down(column, row, area, client) {
            return;
        }
        self.focus_target_at(column, row, area, client);
    }

    pub fn recover_first_visible_comms(&mut self, client: &CcbdClient) {
        let Some(item) = self
            .view()
            .and_then(|view| view.comms.iter().find(|item| item.recoverable))
            .cloned()
        else {
            return;
        };
        self.recover_comms_item(client, &item);
    }

    pub fn restart_project_panes(&mut self, client: &CcbdClient) {
        match client.restart_panes() {
            Ok(()) => self.force_refresh(),
            Err(err) => self.set_error(err),
        }
    }

    pub fn recover_comms_at(
        &mut self,
        column: u16,
        row: u16,
        area: Rect,
        client: &CcbdClient,
    ) -> bool {
        self.handle_comms_mouse_down(column, row, area, client)
    }

    pub fn handle_comms_mouse_down(
        &mut self,
        column: u16,
        row: u16,
        area: Rect,
        client: &CcbdClient,
    ) -> bool {
        let Some((index, action)) = self.comms_action_at(column, row, area) else {
            return false;
        };
        let Some(item) = self.visible_comms().get(index).cloned() else {
            return false;
        };
        self.selected_comms = Some(index);
        match action {
            CommsMouseAction::Retry if item.recoverable => self.recover_comms_item(client, &item),
            CommsMouseAction::Cancel if comms_cancel_enabled(&item) => {
                self.cancel_comms_item(client, &item)
            }
            CommsMouseAction::Clear => self.dismiss_comms_item(client, &item),
            _ => {}
        }
        true
    }

    fn target_index_at(&self, column: u16, row: u16, area: Rect) -> Option<usize> {
        let (tree_area, _) = sidebar_areas(area);
        target_index_at_tree_area(self.targets().len(), tree_area, column, row)
    }

    #[cfg(test)]
    fn comms_index_at(&self, column: u16, row: u16, area: Rect) -> Option<usize> {
        self.comms_action_at(column, row, area)
            .map(|(index, _)| index)
    }

    fn comms_action_at(
        &self,
        column: u16,
        row: u16,
        area: Rect,
    ) -> Option<(usize, CommsMouseAction)> {
        let (_, comms_area) = sidebar_areas(area);
        let prefix_lines = if self.last_error.is_some() { 1 } else { 0 };
        comms_action_at_area(
            &self.visible_comms(),
            comms_area,
            column,
            row,
            usize::from(comms_area.width.saturating_sub(2)),
            prefix_lines,
        )
    }

    fn visible_comms(&self) -> Vec<CommsItem> {
        self.view()
            .map(|view| {
                view.comms
                    .iter()
                    .filter(|item| !self.hidden_comms.contains(&item.id))
                    .cloned()
                    .collect()
            })
            .unwrap_or_default()
    }

    fn view(&self) -> Option<&ProjectView> {
        self.response.as_ref().map(|response| &response.view)
    }

    fn targets(&self) -> Vec<RowTarget> {
        self.view().map(row_targets).unwrap_or_default()
    }

    fn refresh_interval(&self) -> Duration {
        let ttl_ms = self
            .response
            .as_ref()
            .map(|response| response.cache.ttl_ms)
            .filter(|ttl_ms| *ttl_ms > 0)
            .unwrap_or(PROJECT_VIEW_REFRESH_DEFAULT_MS);
        Duration::from_millis(
            ttl_ms.clamp(PROJECT_VIEW_REFRESH_MIN_MS, PROJECT_VIEW_REFRESH_MAX_MS),
        )
    }

    fn clamp_selection(&mut self) {
        let count = self.targets().len();
        if count == 0 {
            self.selected = 0;
        } else if self.selected >= count {
            self.selected = count - 1;
        }
    }

    fn select_active_target(&mut self) {
        if let Some(index) = self.active_target_index() {
            self.selected = index;
        } else {
            self.clamp_selection();
        }
    }

    fn active_target_index(&self) -> Option<usize> {
        let view = self.view()?;
        let targets = row_targets(view);

        if let Some(active_agent) = view.agents.iter().find(|agent| agent.active) {
            let target = RowTarget::Agent(active_agent.name.clone());
            if let Some(index) = targets.iter().position(|candidate| candidate == &target) {
                return Some(index);
            }
        }

        if let Some(active_window) = view.windows.iter().find(|window| window.active) {
            let target = RowTarget::Window(active_window.name.clone());
            if let Some(index) = targets.iter().position(|candidate| candidate == &target) {
                return Some(index);
            }
        }

        let active_window = view
            .namespace
            .active_window
            .as_deref()
            .map(str::trim)
            .filter(|window| !window.is_empty())?;
        let target = RowTarget::Window(active_window.to_string());
        targets.iter().position(|candidate| candidate == &target)
    }

    fn focus_target(&mut self, client: &CcbdClient, target: RowTarget) {
        match request_focus(client, &target, self.namespace_epoch()) {
            Ok(()) => self.force_refresh(),
            Err(err) if is_stale_view_error(&err) => {
                self.retry_focus_after_stale_view(client, target)
            }
            Err(err) => self.set_error(err),
        }
    }

    fn retry_focus_after_stale_view(&mut self, client: &CcbdClient, target: RowTarget) {
        match client.project_view() {
            Ok(response) => self.apply_response(response),
            Err(err) => {
                self.set_error(err);
                return;
            }
        }
        match request_focus(client, &target, self.namespace_epoch()) {
            Ok(()) => self.force_refresh(),
            Err(err) => self.set_error(err),
        }
    }

    fn recover_comms_item(&mut self, client: &CcbdClient, item: &CommsItem) {
        let job_id = recover_job_id(item).unwrap_or(item.id.as_str());
        let reply_delivery_job_id =
            recover_reply_delivery_job_id(item).or(item.reply_delivery_job_id.as_deref());
        match client.comms_recover(job_id, reply_delivery_job_id, item.block_reason.as_deref()) {
            Ok(()) => self.force_refresh(),
            Err(err) => self.set_error(err),
        }
    }

    fn cancel_comms_item(&mut self, client: &CcbdClient, item: &CommsItem) {
        let job_id = recover_job_id(item).unwrap_or(item.id.as_str());
        match client.cancel(job_id) {
            Ok(()) => self.force_refresh(),
            Err(err) if is_terminal_cancel_error(&err) => {
                self.hide_comms_item(item);
                self.force_refresh();
            }
            Err(err) => self.set_error(err),
        }
    }

    fn dismiss_comms_item(&mut self, client: &CcbdClient, item: &CommsItem) {
        match client.dismiss_comms(&item.id) {
            Ok(()) => {
                self.hide_comms_item(item);
                self.force_refresh();
            }
            Err(err) => self.set_error(err),
        }
    }

    fn hide_comms_item(&mut self, item: &CommsItem) {
        if !item.id.trim().is_empty() {
            self.hidden_comms.insert(item.id.clone());
        }
    }
}

fn request_focus(
    client: &CcbdClient,
    target: &RowTarget,
    namespace_epoch: Option<u64>,
) -> Result<(), String> {
    match target {
        RowTarget::Window(window) => client.focus_window(window, namespace_epoch),
        RowTarget::Agent(agent) => client.focus_agent(agent, namespace_epoch),
    }
}

fn is_stale_view_error(error: &str) -> bool {
    error.trim() == "stale_view" || error.contains("stale_view")
}

fn refresh_backoff_for_failures(failure_count: u32) -> Duration {
    if failure_count <= 1 {
        Duration::from_secs(2)
    } else {
        Duration::from_secs(5)
    }
}

pub fn draw(frame: &mut Frame<'_>, app: &SidebarApp) {
    let area = frame.area();
    frame.render_widget(Clear, area);
    let (tree_area, comms_area) = sidebar_areas(area);
    draw_tree(frame, tree_area, app);
    draw_comms(frame, comms_area, app);
}

fn sidebar_areas(area: Rect) -> (Rect, Rect) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Percentage(50), Constraint::Percentage(50)])
        .split(area);
    (chunks[0], chunks[1])
}

fn target_index_at_tree_area(
    target_count: usize,
    area: Rect,
    column: u16,
    row: u16,
) -> Option<usize> {
    if target_count == 0 || area.width < 3 || area.height < 3 {
        return None;
    }
    let left = area.x.saturating_add(1);
    let right = area.x.saturating_add(area.width.saturating_sub(1));
    if column < left || column >= right {
        return None;
    }
    let top = area.y.saturating_add(1);
    let bottom = area.y.saturating_add(area.height.saturating_sub(1));
    if row < top || row >= bottom {
        return None;
    }
    let index = usize::from(row - top);
    if index < target_count {
        Some(index)
    } else {
        None
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CommsMouseAction {
    Select,
    Retry,
    Cancel,
    Clear,
}

fn comms_action_at_area(
    items: &[CommsItem],
    area: Rect,
    column: u16,
    row: u16,
    width: usize,
    prefix_lines: u16,
) -> Option<(usize, CommsMouseAction)> {
    if items.is_empty() || area.width < 3 || area.height < 3 {
        return None;
    }
    let left = area.x.saturating_add(1);
    let right = area.x.saturating_add(area.width.saturating_sub(1));
    if column < left || column >= right {
        return None;
    }
    let top = area.y.saturating_add(1);
    let bottom = area.y.saturating_add(area.height.saturating_sub(1));
    if row < top || row >= bottom {
        return None;
    }
    let mut current = top.saturating_add(prefix_lines);
    for (index, item) in items.iter().enumerate() {
        let height = comms_lines(item, width).len().max(1) as u16;
        if row >= current && row < current.saturating_add(height) {
            let relative_column = column.saturating_sub(left);
            return Some((index, comms_mouse_action_for_column(relative_column)));
        }
        current = current.saturating_add(height);
        if current >= bottom {
            break;
        }
    }
    None
}

fn comms_mouse_action_for_column(column: u16) -> CommsMouseAction {
    if COMMS_ACTION_RETRY_COLS.contains(&column) {
        CommsMouseAction::Retry
    } else if COMMS_ACTION_CANCEL_COLS.contains(&column) {
        CommsMouseAction::Cancel
    } else if COMMS_ACTION_CLEAR_COLS.contains(&column) {
        CommsMouseAction::Clear
    } else {
        CommsMouseAction::Select
    }
}

fn draw_tree(frame: &mut Frame<'_>, area: Rect, app: &SidebarApp) {
    let title = app
        .view()
        .map(|view| tree_title(view, app, tree_title_width(area.width)))
        .unwrap_or_else(|| {
            tree_title_from_parts(
                &app.pane_window,
                None,
                app.last_error.is_some(),
                tree_title_width(area.width),
            )
        });
    let focus_style = tree_focus_style(app);
    let mut rows = Vec::new();
    if let Some(view) = app.view() {
        for window in &view.windows {
            rows.push(window_row(window));
            for agent in view
                .agents
                .iter()
                .filter(|agent| agent.window == window.name)
            {
                rows.push(agent_row(agent));
            }
        }
    }
    if rows.is_empty() {
        rows.push(ListItem::new(Line::from(if app.last_error.is_some() {
            "ccbd unavailable"
        } else {
            "waiting for ProjectView"
        })));
    }
    let items = rows
        .into_iter()
        .enumerate()
        .map(|(index, item)| {
            if index == app.selected {
                item.style(Style::default().add_modifier(Modifier::REVERSED))
            } else {
                item
            }
        })
        .collect::<Vec<_>>();
    let list = List::new(items).block(
        Block::default()
            .title_top(Line::from(title).style(focus_style).left_aligned())
            .title_top(tree_controls_line().right_aligned())
            .borders(Borders::ALL)
            .border_style(focus_style),
    );
    if area.height > 0 {
        frame.render_widget(list, area);
    }
}

#[cfg(test)]
fn tree_controls_area(area: Rect) -> Rect {
    if area.width < TREE_CONTROL_CONTENT_WIDTH + 2 || area.height == 0 {
        return Rect::new(area.x, area.y, 0, 0);
    }
    Rect::new(
        area.x + area.width - TREE_CONTROL_CONTENT_WIDTH - 1,
        area.y,
        TREE_CONTROL_CONTENT_WIDTH,
        1,
    )
}

fn tree_title_width(width: u16) -> u16 {
    width.saturating_sub(TREE_CONTROL_CONTENT_WIDTH + 1)
}

fn tree_controls_line() -> Line<'static> {
    Line::from(vec![
        Span::styled(
            TREE_RESTART_SYMBOL,
            Style::default()
                .fg(Color::Green)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw(" "),
        Span::styled(
            TREE_KILL_SYMBOL,
            Style::default().fg(Color::Red).add_modifier(Modifier::BOLD),
        ),
    ])
}

fn tree_title(view: &ProjectView, app: &SidebarApp, width: u16) -> String {
    tree_title_from_parts(
        &app.pane_window,
        view.namespace.active_window.as_deref(),
        app.last_error.is_some(),
        width,
    )
}

fn tree_title_from_parts(
    pane_window: &str,
    active_window: Option<&str>,
    degraded: bool,
    width: u16,
) -> String {
    let active = active_window
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or(pane_window);
    let cross_window_focus = active != pane_window;
    let title = if cross_window_focus && degraded {
        format!("focus:{active} · ccbd ✕")
    } else if cross_window_focus {
        format!("focus:{active}")
    } else if degraded {
        "ccbd ✕".to_string()
    } else {
        "Sidebar".to_string()
    };
    let available = usize::from(width.saturating_sub(2));
    if title.chars().count() <= available {
        title
    } else if cross_window_focus && degraded {
        "focus ccbd!".to_string()
    } else if cross_window_focus {
        "focus".to_string()
    } else if degraded {
        "ccbd!".to_string()
    } else {
        String::new()
    }
}

fn tree_focus_style(_app: &SidebarApp) -> Style {
    Style::default().fg(Color::DarkGray)
}

fn window_row(window: &WindowView) -> ListItem<'static> {
    let active = if window.active { ">" } else { " " };
    ListItem::new(Line::from(vec![
        Span::raw(format!("{active} ")),
        Span::styled(
            window.name.clone(),
            Style::default().add_modifier(Modifier::BOLD),
        ),
    ]))
}

fn agent_row(agent: &AgentView) -> ListItem<'static> {
    let state = if agent.activity_state.is_empty() {
        "offline"
    } else {
        agent.activity_state.as_str()
    };
    let symbol = agent
        .activity_symbol
        .as_deref()
        .unwrap_or_else(|| activity_symbol(state));
    let active = if agent.active { "*" } else { " " };
    ListItem::new(Line::from(vec![
        Span::raw("  "),
        Span::styled(
            symbol.to_string(),
            Style::default().fg(activity_color(state, agent.activity_color.as_deref())),
        ),
        Span::raw(format!("{active} ")),
        Span::raw(agent.name.clone()),
        Span::raw(format!(" [{}]", agent.provider)),
    ]))
}

fn draw_comms(frame: &mut Frame<'_>, area: Rect, app: &SidebarApp) {
    let mut lines = Vec::new();
    if app.last_error.is_some() {
        lines.push(Line::from(Span::styled(
            if app.view().is_some() {
                "stale ProjectView"
            } else {
                "ccbd unavailable"
            },
            Style::default().fg(Color::Yellow),
        )));
    }
    if app.view().is_some() {
        let comms_capacity = usize::from(area.height.saturating_sub(2))
            .saturating_sub(lines.len())
            .max(1);
        let content_width = usize::from(area.width.saturating_sub(2));
        let visible_comms = app.visible_comms();
        for item in visible_comms.iter() {
            let item_lines = comms_lines(item, content_width);
            if lines.len() + item_lines.len() > comms_capacity {
                break;
            }
            lines.extend(item_lines);
        }
    }
    if lines.is_empty() {
        lines.push(Line::from("no comms"));
    }
    let paragraph =
        Paragraph::new(lines).block(Block::default().title("Comms").borders(Borders::ALL));
    frame.render_widget(paragraph, area);
}

fn empty_dash(value: &str) -> &str {
    if value.trim().is_empty() { "-" } else { value }
}

#[cfg(test)]
fn comms_line_text(item: &CommsItem) -> String {
    comms_lines(item, 80)
        .into_iter()
        .map(|line| {
            line.spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn comms_lines(item: &CommsItem, width: usize) -> Vec<Line<'static>> {
    let status = if item.status_label.trim().is_empty() {
        empty_dash(&item.status)
    } else {
        item.status_label.trim()
    };
    let preview = item.body_preview.trim();
    let reason = comms_reason(item)
        .map(|value| format!(" {value}"))
        .unwrap_or_default();
    let mut first_line_spans = comms_action_spans(item);
    first_line_spans.push(Span::raw(format!(
        "{}>{} ",
        empty_dash(&item.sender),
        empty_dash(&item.target)
    )));
    first_line_spans.push(Span::styled(
        compact_comms_status(status).to_string(),
        Style::default().fg(comms_status_color(item)),
    ));
    let mut lines = vec![Line::from(first_line_spans)];
    if !preview.is_empty() {
        lines.push(Line::from(truncate_comms_preview(preview, width)));
    }
    if !reason.is_empty() {
        lines.push(Line::from(truncate_comms_preview(reason.trim(), width)));
    }
    lines
}

fn comms_action_spans(_item: &CommsItem) -> Vec<Span<'static>> {
    let retry_style = Style::default()
        .fg(Color::Yellow)
        .add_modifier(Modifier::BOLD);
    let cancel_style = Style::default().fg(Color::Red).add_modifier(Modifier::BOLD);
    vec![
        Span::styled("↻ ", retry_style),
        Span::raw(" "),
        Span::styled("X ", cancel_style),
        Span::raw(" "),
        Span::styled(
            "⌫ ",
            Style::default()
                .fg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw(" "),
    ]
}

fn compact_comms_status(value: &str) -> &str {
    match value.trim() {
        "send" | "sending" => "snd",
        "back" | "replying" => "rep",
        "work" | "running" => "run",
        "done" | "completed" | "replied" => "ok",
        "fail" | "failed" | "delivery_failed" => "err",
        "cancelled" | "canceled" => "cnl",
        other => other,
    }
}

fn truncate_comms_preview(value: &str, width: usize) -> String {
    let text = value.trim();
    if width <= 3 {
        return text.chars().take(width).collect();
    }
    if text.chars().count() <= width {
        return text.to_string();
    }
    let head: String = text.chars().take(width.saturating_sub(3)).collect();
    format!("{head}...")
}

fn comms_reason(item: &CommsItem) -> Option<&str> {
    if comms_is_normal_terminal(item) {
        return None;
    }
    item.block_reason
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .or_else(|| {
            item.short_reason
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
        })
}

fn comms_is_normal_terminal(item: &CommsItem) -> bool {
    matches!(item.business_status.trim(), "replied" | "completed")
        || matches!(item.status_label.trim(), "done")
}

fn comms_cancel_enabled(item: &CommsItem) -> bool {
    if item.recoverable {
        return true;
    }
    matches!(
        item.status.trim(),
        "accepted" | "queued" | "running" | "failed" | "incomplete" | "cancelled"
    ) || matches!(
        item.business_status.trim(),
        "sending"
            | "delivering"
            | "blocked"
            | "replying"
            | "failed"
            | "delivery_failed"
            | "incomplete"
    ) || matches!(
        item.status_label.trim(),
        "send" | "back" | "work" | "stuck" | "fail"
    )
}

fn comms_status_color(item: &CommsItem) -> Color {
    match item.business_status.trim() {
        "sending" | "delivering" | "blocked" => Color::Yellow,
        "replying" => Color::Green,
        "replied" | "completed" => Color::Blue,
        "failed" | "delivery_failed" | "incomplete" | "cancelled" => Color::Red,
        _ => match item.status_label.trim() {
            "send" | "back" => Color::Yellow,
            "stuck" => Color::Yellow,
            "work" => Color::Green,
            "done" => Color::Blue,
            "fail" => Color::Red,
            _ => Color::Gray,
        },
    }
}

fn is_terminal_cancel_error(error: &str) -> bool {
    let value = error.trim().to_lowercase();
    value.contains("already terminal")
}

fn recover_job_id(item: &CommsItem) -> Option<&str> {
    item.recover_target
        .as_ref()
        .and_then(|value| value.get("job_id"))
        .and_then(|value| value.as_str())
        .map(str::trim)
        .filter(|value| !value.is_empty())
}

fn recover_reply_delivery_job_id(item: &CommsItem) -> Option<&str> {
    item.recover_target
        .as_ref()
        .and_then(|value| value.get("reply_delivery_job_id"))
        .and_then(|value| value.as_str())
        .map(str::trim)
        .filter(|value| !value.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{NamespaceInfo, ProjectInfo};
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use ratatui::style::Color;
    use serde_json::json;
    #[cfg(unix)]
    use std::io::{BufRead, BufReader, Write};
    #[cfg(unix)]
    use std::os::unix::net::UnixListener;
    #[cfg(unix)]
    use std::sync::{Arc, Mutex};
    #[cfg(unix)]
    use std::thread;

    #[test]
    fn selection_tracks_project_view_rows() {
        let mut app = SidebarApp::new("main".into());
        app.apply_response(sample_response());

        assert_eq!(
            app.selected_target(),
            Some(RowTarget::Agent("agent1".into()))
        );
        app.move_selection(-1);
        assert_eq!(
            app.selected_target(),
            Some(RowTarget::Window("main".into()))
        );
        app.move_selection(1);
        assert_eq!(
            app.selected_target(),
            Some(RowTarget::Agent("agent1".into()))
        );
        assert_eq!(app.namespace_epoch(), Some(1));
    }

    #[test]
    fn selection_follows_active_agent_after_refresh() {
        let mut app = SidebarApp::new("main".into());
        let mut response = sample_response_with_two_agents();
        response.view.agents[0].active = true;
        response.view.agents[1].active = false;
        app.apply_response(response);

        assert_eq!(
            app.selected_target(),
            Some(RowTarget::Agent("agent1".into()))
        );

        let mut response = sample_response_with_two_agents();
        response.view.agents[0].active = false;
        response.view.agents[1].active = true;
        app.apply_response(response);

        assert_eq!(
            app.selected_target(),
            Some(RowTarget::Agent("agent2".into()))
        );
    }

    #[test]
    fn manual_selection_is_preserved_until_focus_is_requested() {
        let mut app = SidebarApp::new("main".into());
        app.apply_response(sample_response_with_two_agents());

        assert_eq!(
            app.selected_target(),
            Some(RowTarget::Agent("agent1".into()))
        );
        app.move_selection(1);
        assert_eq!(
            app.selected_target(),
            Some(RowTarget::Agent("agent2".into()))
        );

        let mut response = sample_response_with_two_agents();
        response.view.agents[0].active = true;
        response.view.agents[1].active = false;
        app.apply_response(response);

        assert_eq!(
            app.selected_target(),
            Some(RowTarget::Agent("agent2".into()))
        );
    }

    #[test]
    fn selection_falls_back_to_active_window_when_no_agent_is_active() {
        let mut app = SidebarApp::new("main".into());
        let mut response = sample_response_with_two_agents();
        response.view.agents[0].active = false;
        response.view.agents[1].active = false;
        app.apply_response(response);

        assert_eq!(
            app.selected_target(),
            Some(RowTarget::Window("main".into()))
        );
    }

    #[test]
    fn selection_falls_back_to_namespace_window() {
        let mut app = SidebarApp::new("main".into());
        let mut response = sample_response_with_two_agents();
        response.view.windows[0].active = false;
        response.view.agents[0].active = false;
        response.view.agents[1].active = false;
        response.view.namespace.active_window = Some("main".into());
        app.apply_response(response);

        assert_eq!(
            app.selected_target(),
            Some(RowTarget::Window("main".into()))
        );
    }

    #[test]
    fn keyboard_selection_can_move_from_focus_synced_agent() {
        let mut app = SidebarApp::new("main".into());
        app.apply_response(sample_response());

        assert_eq!(
            app.selected_target(),
            Some(RowTarget::Agent("agent1".into()))
        );
        app.move_selection(-1);
        assert_eq!(
            app.selected_target(),
            Some(RowTarget::Window("main".into()))
        );
        app.move_selection(1);
        assert_eq!(
            app.selected_target(),
            Some(RowTarget::Agent("agent1".into()))
        );
    }

    #[test]
    fn refresh_interval_respects_project_view_ttl_for_focus_following() {
        let mut app = SidebarApp::new("main".into());
        app.apply_response(sample_response());

        assert_eq!(app.refresh_interval(), Duration::from_millis(1000));
    }

    #[test]
    fn mouse_coordinates_map_to_tree_targets_only() {
        let mut app = SidebarApp::new("main".into());
        app.apply_response(sample_response());
        let area = Rect::new(0, 0, 24, 20);

        assert_eq!(app.target_index_at(1, 1, area), Some(0));
        assert_eq!(app.target_index_at(1, 2, area), Some(1));
        assert_eq!(app.target_index_at(0, 1, area), None);
        assert_eq!(app.target_index_at(1, 0, area), None);
        assert_eq!(app.target_index_at(1, 10, area), None);
    }

    #[test]
    fn renders_project_view_tree_and_comms() {
        let mut app = SidebarApp::new("main".into());
        app.apply_response(sample_response());
        app.move_selection(1);

        let backend = TestBackend::new(80, 14);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| draw(frame, &app)).unwrap();

        let rendered = terminal.backend().to_string();
        assert!(!rendered.contains("repo · main"));
        assert!(rendered.contains("> main"));
        assert!(!rendered.contains("@1"));
        assert!(rendered.contains("◐* agent1 [codex]"));
        assert!(!rendered.contains("#job1"));
        assert!(rendered.contains("Comms"));
        assert!(rendered.contains("↻  X  ⌫  agent2>agent1 run"));
        assert!(rendered.contains("↻ ×"));
        assert!(!rendered.contains("⏻"));
        assert!(!rendered.contains("✚"));
        assert!(!rendered.contains("⟲"));
        assert!(!rendered.contains("Q kill"));

        let buffer = terminal.backend().buffer();
        assert_eq!(buffer[(0, 0)].fg, Color::DarkGray);
        let symbol_cell = buffer
            .content
            .iter()
            .find(|cell| cell.symbol() == "◐")
            .expect("pending status symbol should render");
        assert_eq!(symbol_cell.fg, Color::Yellow);
        let status_cell = buffer
            .content
            .iter()
            .find(|cell| cell.symbol() == "r" && cell.fg == Color::Green)
            .expect("comms status should render");
        assert_eq!(status_cell.fg, Color::Green);
        let retry_cell = buffer
            .content
            .iter()
            .find(|cell| cell.symbol() == "↻" && cell.fg == Color::Yellow)
            .expect("retry action should render");
        assert_eq!(retry_cell.fg, Color::Yellow);
        let cancel_cell = buffer
            .content
            .iter()
            .find(|cell| cell.symbol() == "X")
            .expect("cancel action should render");
        assert_eq!(cancel_cell.fg, Color::Red);
        let clear_cell = buffer
            .content
            .iter()
            .find(|cell| cell.symbol() == "⌫")
            .expect("clear action should render");
        assert_eq!(clear_cell.fg, Color::Cyan);
        let restart_cell = buffer
            .content
            .iter()
            .find(|cell| cell.symbol() == TREE_RESTART_SYMBOL && cell.fg == Color::Green)
            .expect("project restart control should render");
        assert_eq!(restart_cell.fg, Color::Green);
        let kill_cell = buffer
            .content
            .iter()
            .find(|cell| cell.symbol() == TREE_KILL_SYMBOL)
            .expect("project kill control should render");
        assert_eq!(kill_cell.fg, Color::Red);
    }

    #[test]
    fn tree_controls_render_as_inline_symbol_pair() {
        let line = tree_controls_line();
        let text = line
            .spans
            .iter()
            .map(|span| span.content.as_ref())
            .collect::<String>();

        assert_eq!(text, "↻ ×");
    }

    #[test]
    fn tree_controls_area_sits_on_title_bar_right() {
        let area = Rect::new(0, 0, 23, 24);

        assert_eq!(tree_controls_area(area), Rect::new(19, 0, 3, 1));
    }

    #[test]
    fn renders_tree_and_comms_as_half_height_panels() {
        let mut app = SidebarApp::new("main".into());
        app.apply_response(sample_response_with_comms(6));

        let backend = TestBackend::new(80, 20);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| draw(frame, &app)).unwrap();

        let rendered = terminal.backend().to_string();
        assert!(rendered.contains("↻  X  ⌫  agent4>agent1 ok"));
        let buffer = terminal.backend().buffer();
        assert_eq!(buffer[(0, 10)].symbol(), "┌");
        assert_eq!(buffer[(1, 10)].symbol(), "C");
    }

    #[test]
    fn ccb_kill_prefers_sibling_cli_binary() {
        let dir = std::env::temp_dir().join(format!(
            "ccb-agent-sidebar-sibling-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let ccb = dir.join("ccb");
        std::fs::write(&ccb, b"#!/bin/sh\n").unwrap();
        let sidebar = dir.join("ccb-agent-sidebar");

        assert_eq!(ccb_sibling_for_sidebar(&sidebar), Some(ccb));

        let _ = std::fs::remove_dir_all(dir);
    }

    #[cfg(unix)]
    #[test]
    fn restart_panes_calls_ccbd_project_restart_without_exiting_tui() {
        let (socket_path, handle) = spawn_project_restart_server();
        let client = CcbdClient::new(socket_path);
        let mut app = SidebarApp::new("main".into());
        app.apply_response(sample_response());

        app.restart_project_panes(&client);
        handle.join().unwrap();

        assert!(app.last_error.is_none());
        assert!(app.needs_refresh());
    }

    #[cfg(unix)]
    #[test]
    fn restart_panes_action_helper_calls_ccbd_project_restart() {
        let (socket_path, handle) = spawn_project_restart_server();

        run_ccbd_restart_panes(&socket_path).unwrap();
        handle.join().unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn q_kill_runs_ccb_kill_from_project_root() {
        use std::os::unix::fs::PermissionsExt;

        let dir = std::env::temp_dir().join(format!(
            "ccb-agent-sidebar-kill-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let project_root = dir.join("repo");
        let bin_dir = dir.join("bin");
        std::fs::create_dir_all(&project_root).unwrap();
        std::fs::create_dir_all(&bin_dir).unwrap();
        let marker = dir.join("marker");
        let ccb = bin_dir.join("ccb");
        std::fs::write(
            &ccb,
            format!(
                "#!/bin/sh\nprintf '%s|%s\\n' \"$PWD\" \"$1\" > {}\n",
                marker.display()
            ),
        )
        .unwrap();
        let mut permissions = std::fs::metadata(&ccb).unwrap().permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(&ccb, permissions).unwrap();

        run_ccb_kill_with_program(ccb, &project_root).unwrap();

        assert_eq!(
            std::fs::read_to_string(&marker).unwrap(),
            format!("{}|kill\n", project_root.display())
        );
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn tree_header_marks_focus_in_another_window() {
        let mut app = SidebarApp::new("review".into());
        app.apply_response(sample_response());

        let backend = TestBackend::new(80, 14);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| draw(frame, &app)).unwrap();

        let rendered = terminal.backend().to_string();
        assert!(rendered.contains("focus:main"));
        assert!(!rendered.contains("repo · review"));
        assert_eq!(terminal.backend().buffer()[(0, 0)].fg, Color::DarkGray);
    }

    #[test]
    fn narrow_tree_header_prioritizes_cross_window_focus() {
        let mut app = SidebarApp::new("review".into());
        app.apply_response(sample_response());

        let rendered = render_to_string(&app, 24, 14);

        assert!(rendered.contains("focus:main"));
        assert!(!rendered.contains("review>main"));
    }

    #[test]
    fn rpc_failure_keeps_last_good_project_view_and_marks_degraded() {
        let mut app = SidebarApp::new("main".into());
        app.apply_response(sample_response());
        app.set_error("connect /tmp/ccbd.sock: refused".into());

        let rendered = render_to_string(&app, 80, 14);

        assert!(rendered.contains("ccbd ✕"));
        assert!(!rendered.contains("repo · main"));
        assert!(rendered.contains("> main"));
        assert!(rendered.contains("◐* agent1 [codex]"));
        assert!(!rendered.contains("#job1"));
        assert!(rendered.contains("stale ProjectView"));
        assert!(rendered.contains("↻  X  ⌫  agent2>agent1 run"));
        assert!(!rendered.contains("connect /tmp/ccbd.sock"));
    }

    #[test]
    fn rpc_failure_without_last_good_view_renders_minimal_degraded_screen() {
        let mut app = SidebarApp::new("main".into());
        app.set_error("empty response from ccbd".into());

        let rendered = render_to_string(&app, 80, 14);

        assert!(rendered.contains("ccbd ✕"));
        assert!(!rendered.contains("CCB · main"));
        assert!(rendered.contains("ccbd unavailable"));
        assert!(!rendered.contains("agent1"));
        assert!(!rendered.contains("empty response from ccbd"));
    }

    #[test]
    fn rpc_failure_backoff_extends_after_first_failure_and_resets_on_success() {
        let mut app = SidebarApp::new("main".into());

        app.set_error("first".into());
        assert_eq!(app.failure_count, 1);
        assert!(!app.needs_refresh());

        app.set_error("second".into());
        assert_eq!(app.failure_count, 2);
        assert!(!app.needs_refresh());

        app.apply_response(sample_response());
        assert_eq!(app.failure_count, 0);
    }

    #[test]
    fn comms_line_includes_short_id_and_reason_when_present() {
        let item = crate::model::CommsItem {
            short_id: "abcd".into(),
            sender: "agent2".into(),
            target: "agent1".into(),
            status: "failed".into(),
            business_status: "failed".into(),
            status_label: "fail".into(),
            body_preview: "check agent status".into(),
            short_reason: Some("timeout".into()),
            ..Default::default()
        };

        assert_eq!(
            comms_line_text(&item),
            "↻  X  ⌫  agent2>agent1 err\ncheck agent status\ntimeout"
        );
        assert_eq!(comms_status_color(&item), Color::Red);
    }

    #[test]
    fn comms_line_marks_recoverable_items_with_block_reason() {
        let item = crate::model::CommsItem {
            id: "job1".into(),
            short_id: "job1".into(),
            sender: "agent2".into(),
            target: "agent1".into(),
            status: "running".into(),
            business_status: "replying".into(),
            status_label: "work".into(),
            body_preview: "check agent status".into(),
            recoverable: true,
            block_reason: Some("pane_dead".into()),
            recover_target: Some(json!({"job_id": "job1", "reply_delivery_job_id": "job2"})),
            ..Default::default()
        };

        assert_eq!(
            comms_line_text(&item),
            "↻  X  ⌫  agent2>agent1 run\ncheck agent status\npane_dead"
        );
        assert_eq!(recover_job_id(&item), Some("job1"));
        assert_eq!(recover_reply_delivery_job_id(&item), Some("job2"));
    }

    #[test]
    fn comms_line_hides_reason_for_normal_terminal_rows() {
        let item = crate::model::CommsItem {
            short_id: "abcd".into(),
            sender: "agent2".into(),
            target: "agent1".into(),
            status: "completed".into(),
            business_status: "replied".into(),
            status_label: "done".into(),
            body_preview: "all set".into(),
            short_reason: Some("hook_stop".into()),
            ..Default::default()
        };

        assert_eq!(comms_line_text(&item), "↻  X  ⌫  agent2>agent1 ok\nall set");
        assert_eq!(comms_status_color(&item), Color::Blue);
    }

    #[test]
    fn comms_preview_truncates_to_available_width() {
        let item = crate::model::CommsItem {
            sender: "agent2".into(),
            target: "agent1".into(),
            status_label: "done".into(),
            body_preview: "COMMS_BUSINESS_VIEW_OK".into(),
            ..Default::default()
        };
        let rendered = comms_lines(&item, 12)
            .into_iter()
            .map(|line| {
                line.spans
                    .iter()
                    .map(|span| span.content.as_ref())
                    .collect::<String>()
            })
            .collect::<Vec<_>>();

        assert_eq!(
            rendered.as_slice(),
            ["↻  X  ⌫  agent2>agent1 ok", "COMMS_BUS..."]
        );
    }

    #[test]
    fn mouse_coordinates_map_to_comms_rows() {
        let mut app = SidebarApp::new("main".into());
        let mut response = sample_response_with_comms(3);
        response.view.comms[0].body_preview = "line two".into();
        response.view.comms[1].body_preview = "line two".into();
        app.apply_response(response);
        let area = Rect::new(0, 0, 24, 20);

        assert_eq!(app.comms_index_at(1, 11, area), Some(0));
        assert_eq!(app.comms_index_at(1, 13, area), Some(1));
        assert_eq!(app.comms_index_at(0, 11, area), None);
        assert_eq!(app.comms_index_at(1, 9, area), None);
    }

    #[test]
    fn comms_mouse_action_columns_are_fixed() {
        let item = crate::model::CommsItem {
            id: "msg1".into(),
            sender: "agent2".into(),
            target: "agent1".into(),
            status_label: "work".into(),
            body_preview: "line two".into(),
            ..Default::default()
        };
        let area = Rect::new(0, 10, 24, 10);

        assert_eq!(
            comms_action_at_area(&[item], area, 1, 11, 22, 0),
            Some((0, CommsMouseAction::Retry))
        );
        assert_eq!(
            comms_action_at_area(&[sample_comms_item("msg1")], area, 3, 11, 22, 0),
            Some((0, CommsMouseAction::Select))
        );
        assert_eq!(
            comms_action_at_area(&[sample_comms_item("msg1")], area, 4, 11, 22, 0),
            Some((0, CommsMouseAction::Cancel))
        );
        assert_eq!(
            comms_action_at_area(&[sample_comms_item("msg1")], area, 7, 11, 22, 0),
            Some((0, CommsMouseAction::Clear))
        );
        assert_eq!(
            comms_action_at_area(&[sample_comms_item("msg1")], area, 10, 11, 22, 0),
            Some((0, CommsMouseAction::Select))
        );
    }

    #[cfg(unix)]
    #[test]
    fn clicking_plain_comms_row_is_consumed_for_future_details() {
        let client = CcbdClient::new("/tmp/not-used.sock");
        let mut app = SidebarApp::new("main".into());
        let mut response = sample_response();
        response.view.comms[0].recoverable = false;
        response.view.comms[0].target = "agent1".into();
        app.apply_response(response);
        let area = Rect::new(0, 0, 24, 20);

        assert!(app.handle_comms_mouse_down(10, 11, area, &client));

        assert!(app.last_error.is_none());
        assert!(!app.needs_refresh());
        assert_eq!(app.selected_comms, Some(0));
    }

    #[cfg(unix)]
    #[test]
    fn clicking_retry_symbol_calls_ccbd() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let (socket_path, handle) = spawn_comms_recover_server(Arc::clone(&seen));
        let client = CcbdClient::new(socket_path);
        let mut app = SidebarApp::new("main".into());
        let mut response = sample_response();
        response.view.comms[0].recoverable = true;
        response.view.comms[0].recover_target =
            Some(json!({"job_id": "job1", "reply_delivery_job_id": "job2"}));
        response.view.comms[0].block_reason = Some("provider_prompt_idle".into());
        app.apply_response(response);
        let area = Rect::new(0, 0, 24, 20);

        assert!(app.handle_comms_mouse_down(1, 11, area, &client));
        handle.join().unwrap();

        assert!(app.last_error.is_none());
        assert!(app.needs_refresh());
        assert_eq!(app.selected_comms, Some(0));
        assert_eq!(
            seen.lock().unwrap().as_slice(),
            ["comms_recover:job1:job2:provider_prompt_idle"]
        );
    }

    #[cfg(unix)]
    #[test]
    fn clicking_cancel_symbol_calls_ccbd_cancel() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let (socket_path, handle) = spawn_cancel_server(Arc::clone(&seen));
        let client = CcbdClient::new(socket_path);
        let mut app = SidebarApp::new("main".into());
        app.apply_response(sample_response());
        let area = Rect::new(0, 0, 24, 20);

        assert!(app.handle_comms_mouse_down(4, 11, area, &client));
        handle.join().unwrap();

        assert!(app.last_error.is_none());
        assert!(app.needs_refresh());
        assert_eq!(seen.lock().unwrap().as_slice(), ["cancel:msg1"]);
    }

    #[cfg(unix)]
    #[test]
    fn terminal_cancel_error_hides_comms_row_locally() {
        let (socket_path, handle) =
            spawn_error_server("cancel", "job is already terminal: completed");
        let client = CcbdClient::new(socket_path);
        let mut app = SidebarApp::new("main".into());
        app.apply_response(sample_response());
        let area = Rect::new(0, 0, 24, 20);

        assert!(app.handle_comms_mouse_down(4, 11, area, &client));
        handle.join().unwrap();

        assert!(app.last_error.is_none());
        assert!(app.needs_refresh());
        assert_eq!(app.visible_comms(), Vec::<CommsItem>::new());
    }

    #[cfg(unix)]
    #[test]
    fn clicking_clear_symbol_dismisses_comms_through_ccbd() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let (socket_path, handle) = spawn_dismiss_comms_server(Arc::clone(&seen));
        let client = CcbdClient::new(socket_path);
        let mut app = SidebarApp::new("main".into());
        app.apply_response(sample_response());
        let area = Rect::new(0, 0, 24, 20);

        assert!(app.handle_comms_mouse_down(7, 11, area, &client));
        handle.join().unwrap();

        assert!(app.last_error.is_none());
        assert!(app.needs_refresh());
        assert_eq!(app.visible_comms(), Vec::<CommsItem>::new());
        assert_eq!(seen.lock().unwrap().as_slice(), ["dismiss:msg1"]);
    }

    #[cfg(unix)]
    #[test]
    fn recover_first_visible_comms_calls_ccbd() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let (socket_path, handle) = spawn_comms_recover_server(Arc::clone(&seen));
        let client = CcbdClient::new(socket_path);
        let mut app = SidebarApp::new("main".into());
        let mut response = sample_response();
        response.view.comms[0].recoverable = true;
        response.view.comms[0].recover_target =
            Some(json!({"job_id": "job1", "reply_delivery_job_id": "job2"}));
        response.view.comms[0].block_reason = Some("provider_prompt_idle".into());
        app.apply_response(response);

        app.recover_first_visible_comms(&client);
        handle.join().unwrap();

        assert!(app.last_error.is_none());
        assert!(app.needs_refresh());
        assert_eq!(
            seen.lock().unwrap().as_slice(),
            ["comms_recover:job1:job2:provider_prompt_idle"]
        );
    }

    #[cfg(unix)]
    #[test]
    fn stale_view_focus_refreshes_and_retries_once() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let (socket_path, handle) = spawn_stale_focus_server(Arc::clone(&seen));
        let client = CcbdClient::new(socket_path);
        let mut app = SidebarApp::new("main".into());
        app.apply_response(sample_response());
        app.move_selection(1);

        app.focus_selected_target(&client);
        handle.join().unwrap();

        assert!(app.last_error.is_none());
        assert!(app.needs_refresh());
        assert_eq!(app.namespace_epoch(), Some(2));
        let seen = seen.lock().unwrap();
        assert_eq!(
            seen.as_slice(),
            [
                "project_focus_agent:1",
                "project_view",
                "project_focus_agent:2"
            ]
        );
    }

    #[cfg(unix)]
    #[test]
    fn target_missing_focus_failure_does_not_refresh_or_retry() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let (socket_path, handle) = spawn_target_missing_server(Arc::clone(&seen));
        let client = CcbdClient::new(socket_path);
        let mut app = SidebarApp::new("main".into());
        app.apply_response(sample_response());
        app.move_selection(1);

        app.focus_selected_target(&client);
        handle.join().unwrap();

        assert_eq!(app.last_error.as_deref(), Some("target_missing"));
        assert_eq!(app.namespace_epoch(), Some(1));
        assert_eq!(seen.lock().unwrap().as_slice(), ["project_focus_agent"]);
    }

    #[cfg(unix)]
    #[test]
    fn enter_on_window_row_focuses_window_through_ccbd() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let (socket_path, handle) = spawn_window_focus_server(Arc::clone(&seen));
        let client = CcbdClient::new(socket_path);
        let mut app = SidebarApp::new("main".into());
        app.apply_response(sample_response());
        app.move_selection(-1);

        app.focus_selected_target(&client);
        handle.join().unwrap();

        assert!(app.last_error.is_none());
        assert!(app.needs_refresh());
        assert_eq!(
            seen.lock().unwrap().as_slice(),
            ["project_focus_window:main:1"]
        );
    }

    #[cfg(unix)]
    #[test]
    fn tab_focuses_the_sidebar_window_through_ccbd() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let (socket_path, handle) = spawn_window_focus_server(Arc::clone(&seen));
        let client = CcbdClient::new(socket_path);
        let mut app = SidebarApp::new("ops".into());
        app.apply_response(sample_response());

        app.focus_pane_window(&client);
        handle.join().unwrap();

        assert!(app.last_error.is_none());
        assert!(app.needs_refresh());
        assert_eq!(
            seen.lock().unwrap().as_slice(),
            ["project_focus_window:ops:1"]
        );
    }

    fn sample_response() -> ProjectViewResponse {
        ProjectViewResponse {
            view: ProjectView {
                project: ProjectInfo {
                    display_name: "repo".into(),
                    root: "/repo".into(),
                },
                namespace: NamespaceInfo {
                    epoch: Some(1),
                    active_window: Some("main".into()),
                    entry_window: "main".into(),
                },
                windows: vec![WindowView {
                    name: "main".into(),
                    active: true,
                    tmux_window_id: Some("@1".into()),
                    ..WindowView::default()
                }],
                agents: vec![AgentView {
                    name: "agent1".into(),
                    provider: "codex".into(),
                    window: "main".into(),
                    active: true,
                    activity_state: "pending".into(),
                    activity_symbol: Some("◐".into()),
                    activity_color: Some("yellow".into()),
                    ..AgentView::default()
                }],
                comms: vec![crate::model::CommsItem {
                    id: "msg1".into(),
                    short_id: "msg1".into(),
                    sender: "agent2".into(),
                    target: "agent1".into(),
                    status: "running".into(),
                    business_status: "replying".into(),
                    status_label: "work".into(),
                    ..Default::default()
                }],
            },
            cache: Default::default(),
        }
    }

    fn sample_response_with_two_agents() -> ProjectViewResponse {
        let mut response = sample_response();
        response.view.windows[0].agents = vec!["agent1".into(), "agent2".into()];
        response.view.agents.push(AgentView {
            name: "agent2".into(),
            provider: "claude".into(),
            window: "main".into(),
            active: false,
            activity_state: "idle".into(),
            activity_symbol: Some("●".into()),
            activity_color: Some("green".into()),
            ..AgentView::default()
        });
        response
    }

    fn sample_response_with_comms(count: usize) -> ProjectViewResponse {
        let mut response = sample_response();
        response.view.comms = (1..=count)
            .map(|index| crate::model::CommsItem {
                id: format!("msg{index}"),
                short_id: format!("msg{index}"),
                sender: format!("agent{index}"),
                target: "agent1".into(),
                status: "completed".into(),
                status_label: "done".into(),
                ..Default::default()
            })
            .collect();
        response
    }

    fn sample_comms_item(id: &str) -> CommsItem {
        CommsItem {
            id: id.into(),
            sender: "agent2".into(),
            target: "agent1".into(),
            status_label: "work".into(),
            body_preview: "line two".into(),
            ..Default::default()
        }
    }

    fn render_to_string(app: &SidebarApp, width: u16, height: u16) -> String {
        let backend = TestBackend::new(width, height);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| draw(frame, app)).unwrap();
        terminal.backend().to_string()
    }

    #[cfg(unix)]
    fn spawn_stale_focus_server(
        seen: Arc<Mutex<Vec<String>>>,
    ) -> (std::path::PathBuf, thread::JoinHandle<()>) {
        let dir = std::env::temp_dir().join(format!(
            "ccb-agent-sidebar-stale-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let socket_path = dir.join("ccbd.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        let path_for_thread = socket_path.clone();
        let handle = thread::spawn(move || {
            for index in 0..3 {
                let (mut stream, _) = listener.accept().unwrap();
                let mut line = String::new();
                {
                    let mut reader = BufReader::new(&stream);
                    reader.read_line(&mut line).unwrap();
                }
                let request: serde_json::Value = serde_json::from_str(line.trim()).unwrap();
                let op = request["op"].as_str().unwrap();
                let response = match (index, op) {
                    (0, "project_focus_agent") => {
                        seen.lock().unwrap().push(format!(
                            "{op}:{}",
                            request["request"]["namespace_epoch"].as_u64().unwrap()
                        ));
                        json!({"api_version": 2, "ok": false, "error": "stale_view"})
                    }
                    (1, "project_view") => {
                        seen.lock().unwrap().push(op.into());
                        project_view_response_with_epoch(2)
                    }
                    (2, "project_focus_agent") => {
                        seen.lock().unwrap().push(format!(
                            "{op}:{}",
                            request["request"]["namespace_epoch"].as_u64().unwrap()
                        ));
                        json!({"api_version": 2, "ok": true, "focus": {"kind": "agent"}})
                    }
                    _ => panic!("unexpected request {index}: {request}"),
                };
                stream
                    .write_all(format!("{response}\n").as_bytes())
                    .unwrap();
            }
            let _ = std::fs::remove_file(path_for_thread);
            let _ = std::fs::remove_dir(dir);
        });
        (socket_path, handle)
    }

    #[cfg(unix)]
    fn spawn_target_missing_server(
        seen: Arc<Mutex<Vec<String>>>,
    ) -> (std::path::PathBuf, thread::JoinHandle<()>) {
        let dir = std::env::temp_dir().join(format!(
            "ccb-agent-sidebar-target-missing-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let socket_path = dir.join("ccbd.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        let path_for_thread = socket_path.clone();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut line = String::new();
            {
                let mut reader = BufReader::new(&stream);
                reader.read_line(&mut line).unwrap();
            }
            let request: serde_json::Value = serde_json::from_str(line.trim()).unwrap();
            assert_eq!(request["op"], "project_focus_agent");
            seen.lock().unwrap().push("project_focus_agent".into());
            stream
                .write_all(
                    format!(
                        "{}\n",
                        json!({"api_version": 2, "ok": false, "error": "target_missing"})
                    )
                    .as_bytes(),
                )
                .unwrap();
            let _ = std::fs::remove_file(path_for_thread);
            let _ = std::fs::remove_dir(dir);
        });
        (socket_path, handle)
    }

    #[cfg(unix)]
    fn spawn_window_focus_server(
        seen: Arc<Mutex<Vec<String>>>,
    ) -> (std::path::PathBuf, thread::JoinHandle<()>) {
        let dir = std::env::temp_dir().join(format!(
            "ccb-agent-sidebar-window-focus-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let socket_path = dir.join("ccbd.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        let path_for_thread = socket_path.clone();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut line = String::new();
            {
                let mut reader = BufReader::new(&stream);
                reader.read_line(&mut line).unwrap();
            }
            let request: serde_json::Value = serde_json::from_str(line.trim()).unwrap();
            assert_eq!(request["op"], "project_focus_window");
            seen.lock().unwrap().push(format!(
                "project_focus_window:{}:{}",
                request["request"]["window"].as_str().unwrap(),
                request["request"]["namespace_epoch"].as_u64().unwrap()
            ));
            stream
                .write_all(
                    format!(
                        "{}\n",
                        json!({"api_version": 2, "ok": true, "focus": {"kind": "window"}})
                    )
                    .as_bytes(),
                )
                .unwrap();
            let _ = std::fs::remove_file(path_for_thread);
            let _ = std::fs::remove_dir(dir);
        });
        (socket_path, handle)
    }

    #[cfg(unix)]
    fn spawn_comms_recover_server(
        seen: Arc<Mutex<Vec<String>>>,
    ) -> (std::path::PathBuf, thread::JoinHandle<()>) {
        let dir = std::env::temp_dir().join(format!(
            "ccb-agent-sidebar-comms-recover-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let socket_path = dir.join("ccbd.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        let path_for_thread = socket_path.clone();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut line = String::new();
            {
                let mut reader = BufReader::new(&stream);
                reader.read_line(&mut line).unwrap();
            }
            let request: serde_json::Value = serde_json::from_str(line.trim()).unwrap();
            assert_eq!(request["op"], "comms_recover");
            seen.lock().unwrap().push(format!(
                "comms_recover:{}:{}:{}",
                request["request"]["job_id"].as_str().unwrap(),
                request["request"]["reply_delivery_job_id"]
                    .as_str()
                    .unwrap(),
                request["request"]["block_reason"].as_str().unwrap_or("")
            ));
            stream
                .write_all(
                    format!(
                        "{}\n",
                        json!({"api_version": 2, "ok": true, "status": "recovered"})
                    )
                    .as_bytes(),
                )
                .unwrap();
            let _ = std::fs::remove_file(path_for_thread);
            let _ = std::fs::remove_dir(dir);
        });
        (socket_path, handle)
    }

    #[cfg(unix)]
    fn spawn_project_restart_server() -> (std::path::PathBuf, thread::JoinHandle<()>) {
        let dir = std::env::temp_dir().join(format!(
            "ccb-agent-sidebar-restart-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let socket_path = dir.join("ccbd.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        let path_for_thread = socket_path.clone();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut line = String::new();
            {
                let mut reader = BufReader::new(&stream);
                reader.read_line(&mut line).unwrap();
            }
            let request: serde_json::Value = serde_json::from_str(line.trim()).unwrap();
            assert_eq!(request["op"], "project_restart_panes");
            assert_eq!(request["request"], json!({}));
            stream
                .write_all(
                    format!(
                        "{}\n",
                        json!({"api_version": 2, "ok": true, "status": "scheduled"})
                    )
                    .as_bytes(),
                )
                .unwrap();
            let _ = std::fs::remove_file(path_for_thread);
            let _ = std::fs::remove_dir(dir);
        });
        (socket_path, handle)
    }

    #[cfg(unix)]
    fn spawn_cancel_server(
        seen: Arc<Mutex<Vec<String>>>,
    ) -> (std::path::PathBuf, thread::JoinHandle<()>) {
        let dir = std::env::temp_dir().join(format!(
            "ccb-agent-sidebar-cancel-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let socket_path = dir.join("ccbd.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        let path_for_thread = socket_path.clone();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut line = String::new();
            {
                let mut reader = BufReader::new(&stream);
                reader.read_line(&mut line).unwrap();
            }
            let request: serde_json::Value = serde_json::from_str(line.trim()).unwrap();
            assert_eq!(request["op"], "cancel");
            seen.lock().unwrap().push(format!(
                "cancel:{}",
                request["request"]["job_id"].as_str().unwrap()
            ));
            stream
                .write_all(
                    format!(
                        "{}\n",
                        json!({"api_version": 2, "ok": true, "status": "cancelled"})
                    )
                    .as_bytes(),
                )
                .unwrap();
            let _ = std::fs::remove_file(path_for_thread);
            let _ = std::fs::remove_dir(dir);
        });
        (socket_path, handle)
    }

    #[cfg(unix)]
    fn spawn_dismiss_comms_server(
        seen: Arc<Mutex<Vec<String>>>,
    ) -> (std::path::PathBuf, thread::JoinHandle<()>) {
        let dir = std::env::temp_dir().join(format!(
            "ccb-agent-sidebar-dismiss-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let socket_path = dir.join("ccbd.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        let path_for_thread = socket_path.clone();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut line = String::new();
            {
                let mut reader = BufReader::new(&stream);
                reader.read_line(&mut line).unwrap();
            }
            let request: serde_json::Value = serde_json::from_str(line.trim()).unwrap();
            assert_eq!(request["op"], "project_view_dismiss_comms");
            seen.lock().unwrap().push(format!(
                "dismiss:{}",
                request["request"]["id"].as_str().unwrap()
            ));
            stream
                .write_all(
                    format!(
                        "{}\n",
                        json!({"api_version": 2, "ok": true, "status": "dismissed"})
                    )
                    .as_bytes(),
                )
                .unwrap();
            let _ = std::fs::remove_file(path_for_thread);
            let _ = std::fs::remove_dir(dir);
        });
        (socket_path, handle)
    }

    #[cfg(unix)]
    fn spawn_error_server(
        op: &'static str,
        error: &'static str,
    ) -> (std::path::PathBuf, thread::JoinHandle<()>) {
        let dir = std::env::temp_dir().join(format!(
            "ccb-agent-sidebar-error-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let socket_path = dir.join("ccbd.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        let path_for_thread = socket_path.clone();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut line = String::new();
            {
                let mut reader = BufReader::new(&stream);
                reader.read_line(&mut line).unwrap();
            }
            let request: serde_json::Value = serde_json::from_str(line.trim()).unwrap();
            assert_eq!(request["op"], op);
            stream
                .write_all(
                    format!(
                        "{}\n",
                        json!({"api_version": 2, "ok": false, "error": error})
                    )
                    .as_bytes(),
                )
                .unwrap();
            let _ = std::fs::remove_file(path_for_thread);
            let _ = std::fs::remove_dir(dir);
        });
        (socket_path, handle)
    }

    #[cfg(unix)]
    fn project_view_response_with_epoch(epoch: u64) -> serde_json::Value {
        json!({
            "api_version": 2,
            "ok": true,
            "view": {
                "project": {"display_name": "repo", "root": "/repo"},
                "namespace": {"epoch": epoch, "active_window": "main", "entry_window": "main"},
                "windows": [{"name": "main", "active": true, "tmux_window_id": "@1"}],
                "agents": [{
                    "name": "agent1",
                    "provider": "codex",
                    "window": "main",
                    "active": true,
                    "activity_state": "pending",
                    "activity_symbol": "◐",
                    "activity_color": "yellow"
                }],
                "comms": []
            },
            "cache": {"sequence": epoch, "ttl_ms": 1000}
        })
    }
}
