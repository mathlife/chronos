#!/usr/bin/env python3
"""Web dashboard for Chronos settings and tasks."""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import re
import sqlite3
import sys
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import inspect_config, update_raw_config
from core.integration_api import create_task, delete_channel, put_channel, remove_task, update_task
from core.observability import METRICS, emit_log
from core.occurrence_state import OccurrenceStateStore, iter_job_refs, iter_job_refs_from_pair
from core.paths import TODO_DB
from core.system_scheduler import remove_job, supports_system_scheduler
from core.timezones import get_shanghai_tz

SHANGHAI_TZ = get_shanghai_tz()
_TIME_OF_DAY_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

HTML_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Chronos Dashboard</title>
  <style>
    :root { color-scheme: light; --bg:#f5f7fb; --card:#ffffff; --line:#d7deeb; --text:#1e2838; --muted:#64748b; --accent:#0f766e; --warn:#b45309; --ok:#166534; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; background: linear-gradient(160deg,#f7fafc,#eef3ff); color:var(--text); }
    .wrap { max-width:1200px; margin:0 auto; padding:20px; }
    h1 { margin:0 0 8px; font-size:28px; }
    .meta { color:var(--muted); margin-bottom:14px; }
    .grid { display:grid; grid-template-columns: repeat(auto-fit,minmax(320px,1fr)); gap:14px; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; box-shadow:0 6px 14px rgba(12,20,40,0.04); }
    h2 { margin:0 0 10px; font-size:18px; }
    table { width:100%; border-collapse: collapse; font-size:13px; }
    th, td { text-align:left; border-bottom:1px solid #eef2f8; padding:6px 4px; vertical-align: top; }
    th { color:#334155; font-weight:600; }
    .channel-list { display:grid; gap:10px; }
    .channel-item { border:1px solid #e6edf7; border-radius:10px; background:#fbfdff; padding:10px; }
    .channel-head { display:flex; justify-content:space-between; align-items:flex-start; gap:10px; }
    .channel-title { font-size:14px; font-weight:600; color:#22314a; margin-bottom:4px; }
    .channel-sub { font-size:12px; color:#64748b; display:flex; gap:8px; flex-wrap:wrap; }
    .channel-tag { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; }
    .channel-tag.on { background:#dcfce7; color:#166534; }
    .channel-tag.off { background:#fef3c7; color:#92400e; }
    .channel-config { margin-top:8px; border:1px solid #e8edf5; border-radius:8px; background:#f8fafc; padding:8px; }
    .channel-row { display:grid; grid-template-columns: 140px 1fr; gap:8px; padding:3px 0; font-size:12px; }
    .channel-key { color:#475569; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .channel-val { color:#0f172a; word-break: break-all; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    @media (max-width: 700px) { .channel-row { grid-template-columns: 1fr; } }
    .pill { display:inline-block; padding:2px 8px; border-radius:999px; background:#ecfeff; color:var(--accent); font-size:12px; }
    .warn { color:var(--warn); }
    .ok { color:var(--ok); }
    .muted { color:var(--muted); }
    pre { margin:0; white-space:pre-wrap; word-break: break-word; font-size:12px; background:#f8fafc; border:1px solid #e8edf5; border-radius:10px; padding:8px; }
    textarea,input,select { width:100%; border:1px solid #ced7e8; border-radius:8px; padding:8px; font-size:13px; }
    textarea { min-height:90px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .row { display:grid; grid-template-columns: 1fr 1fr; gap:8px; }
    .btns { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
    button { border:0; border-radius:8px; padding:7px 12px; background:#0f766e; color:#fff; cursor:pointer; }
    button.alt { background:#334155; }
    button.warn { background:#b45309; color:#fff; }
    #opResult { margin-top:8px; font-size:13px; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Chronos Dashboard</h1>
    <div class="meta" id="meta">loading...</div>
    <div class="grid">
      <section class="card">
        <h2>Runtime Settings</h2>
        <div id="settings"></div>
      </section>
      <section class="card">
        <h2>Channels</h2>
        <div id="channels"></div>
      </section>
      <section class="card" style="grid-column:1/-1;">
        <h2>Edit Guard</h2>
        <div class="row">
          <div>
            <label><input type="checkbox" id="editMode" /> Enable edit mode in browser</label>
            <div class="muted">Server read-only mode still has priority.</div>
          </div>
          <div class="muted" id="editState"></div>
        </div>
      </section>
      <section class="card" style="grid-column:1/-1;">
        <h2>Config & Channel Ops</h2>
        <div class="row">
          <div>
            <label>Legacy chat_id (optional)</label>
            <input id="legacyChatId" placeholder="e.g. 123456" />
            <div class="btns">
              <button onclick="updateSettings()">Update chat_id</button>
            </div>
          </div>
          <div>
            <label>Edit existing channel</label>
            <select id="editChannelSelect"></select>
            <div class="btns">
              <button class="alt" onclick="loadSelectedChannel()">Load selected channel</button>
              <button class="alt" onclick="startCreateChannel()">Create new channel mode</button>
            </div>
          </div>
        </div>
        <label style="margin-top:8px; display:block;">Channel editor (form only)</label>
        <div class="row">
          <div>
            <label>Channel id</label>
            <input id="channelFormId" placeholder="e.g. tg-main / hook-main" />
          </div>
          <div>
            <label>Channel type</label>
            <select id="channelFormType">
              <option value="telegram">telegram</option>
              <option value="webhook">webhook</option>
            </select>
          </div>
        </div>
        <div class="row" style="margin-top:8px;">
          <div>
            <label>Enabled</label>
            <select id="channelFormEnabled">
              <option value="1">enabled</option>
              <option value="0">disabled</option>
            </select>
          </div>
          <div></div>
        </div>
        <div class="row" style="margin-top:8px;">
          <div>
            <label>Telegram bot_token</label>
            <input id="channelFormBotToken" placeholder="required for telegram" />
          </div>
          <div>
            <label>Telegram chat_id</label>
            <input id="channelFormChatId" placeholder="required for telegram" />
          </div>
        </div>
        <div class="row" style="margin-top:8px;">
          <div>
            <label>Webhook URL</label>
            <input id="channelFormUrl" placeholder="required for webhook" />
          </div>
          <div>
            <label>Webhook secret (optional)</label>
            <input id="channelFormSecret" placeholder="optional" />
          </div>
        </div>
        <div class="btns">
          <button class="alt" onclick="upsertChannelFromForm()">Save channel</button>
          <button class="warn" onclick="removeSelectedChannel()">Remove selected channel</button>
        </div>
        <div id="opResult"></div>
      </section>
      <section class="card" style="grid-column:1/-1;">
        <h2>Task Ops</h2>
        <div class="row">
          <div>
            <label>Edit existing task</label>
            <select id="editTaskSelect"></select>
          </div>
          <div>
            <label>Task selection helper</label>
            <div class="btns">
              <button class="alt" onclick="loadSelectedTask()">Load selected task</button>
              <button class="alt" onclick="startCreateTask()">Create new task mode</button>
            </div>
          </div>
        </div>
        <label style="margin-top:8px; display:block;">Original task JSON (read-only)</label>
        <textarea id="originalTaskJson" readonly>{}</textarea>
        <label style="margin-top:8px; display:block;">Task editor (form only)</label>
        <div class="row">
          <div>
            <label>Task name</label>
            <input id="taskFormName" placeholder="task name" />
          </div>
          <div>
            <label>Task kind</label>
            <select id="taskFormTaskKind">
              <option value="scheduled">scheduled</option>
              <option value="system">system</option>
            </select>
          </div>
        </div>
        <div class="row" style="margin-top:8px;">
          <div>
            <label>Cycle type</label>
            <select id="taskFormCycleType">
              <option value="once">once</option>
              <option value="daily">daily</option>
              <option value="hourly">hourly</option>
              <option value="weekly">weekly</option>
              <option value="monthly_dates">monthly_dates (fixed days)</option>
              <option value="monthly_range">monthly_range (window)</option>
            </select>
          </div>
          <div>
            <label>Time of day (activity start)</label>
            <input id="taskFormTime" type="time" />
          </div>
        </div>
        <div class="row" style="margin-top:8px;">
          <div>
            <label>Start date</label>
            <input id="taskFormStartDate" type="date" />
          </div>
          <div>
            <label>End date</label>
            <input id="taskFormEndDate" type="date" />
          </div>
        </div>
        <div class="row" style="margin-top:8px;">
          <div>
            <label>Weekday (0-6, weekly)</label>
            <input id="taskFormWeekday" type="number" min="0" max="6" />
          </div>
          <div>
            <label>Interval hours (1-24, hourly)</label>
            <input id="taskFormIntervalHours" type="number" min="1" max="24" />
          </div>
        </div>
        <div class="row" style="margin-top:8px;">
          <div>
            <label>Monthly dates list (for monthly_dates)</label>
            <input id="taskFormDatesList" placeholder="e.g. 1,10,25" />
          </div>
          <div>
            <label>Quota in cycle (n_per_month, optional)</label>
            <input id="taskFormNPerMonth" type="number" min="1" placeholder="e.g. 3" />
          </div>
        </div>
        <div class="row" style="margin-top:8px;">
          <div>
            <label>Window start day (for monthly_range)</label>
            <input id="taskFormRangeStart" type="number" min="1" max="31" />
          </div>
          <div>
            <label>Window end day (for monthly_range)</label>
            <input id="taskFormRangeEnd" type="number" min="1" max="31" />
          </div>
        </div>
        <div class="row" style="margin-top:8px;">
          <div>
            <label>Delivery target (comma-separated)</label>
            <input id="taskFormDeliveryTarget" placeholder="tg-main,hook-main" />
          </div>
          <div>
            <label>Active</label>
            <select id="taskFormIsActive">
              <option value="1">active</option>
              <option value="0">inactive</option>
            </select>
          </div>
        </div>
        <div class="btns">
          <button onclick="createTaskFromForm()">Create task</button>
          <button class="alt" onclick="updateSelectedTaskByForm()">Save selected task</button>
          <button class="warn" onclick="removeSelectedTask(false)">Deactivate selected task</button>
          <button class="warn" onclick="removeSelectedTask(true)">Hard delete selected task</button>
        </div>
      </section>
      <section class="card" style="grid-column:1/-1;">
        <h2>Today Tasks</h2>
        <div id="today"></div>
        <label style="margin-top:8px; display:block;">Today task editor</label>
        <div class="row">
          <div>
            <label>Select today task</label>
            <select id="todayTaskSelect"></select>
          </div>
          <div>
            <label>Identifier</label>
            <input id="todayTaskIdentifier" readonly />
          </div>
        </div>
        <div class="row" style="margin-top:8px;">
          <div>
            <label>Name</label>
            <input id="todayFormName" placeholder="task/entry name" />
          </div>
          <div>
            <label>Status</label>
            <select id="todayFormStatus">
              <option value="pending">pending</option>
              <option value="in_progress">in_progress</option>
              <option value="reminded">reminded</option>
              <option value="completed">completed</option>
              <option value="skipped">skipped</option>
            </select>
          </div>
        </div>
        <div class="row" style="margin-top:8px;">
          <div>
            <label>Scheduled time (FIN only, HH:MM)</label>
            <input id="todayFormTime" type="time" />
          </div>
          <div>
            <label>Type</label>
            <input id="todayTaskType" readonly />
          </div>
        </div>
        <div class="btns">
          <button class="alt" onclick="updateSelectedTodayTask()">Save selected today task</button>
          <button class="warn" onclick="removeSelectedTodayTask()">Delete selected today task</button>
        </div>
      </section>
      <section class="card" style="grid-column:1/-1;">
        <h2>All Periodic Tasks</h2>
        <div id="tasks"></div>
      </section>
    </div>
  </div>
  <script>
    const API_BASE = '/api/v1';
    const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    function table(headers, rows) {
      if (!rows.length) return '<div class="muted">empty</div>';
      const th = '<tr>' + headers.map(h => `<th>${esc(h)}</th>`).join('') + '</tr>';
      const tr = rows.map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('');
      return `<table>${th}${tr}</table>`;
    }
    function maskChannelConfigValue(key, value) {
      const normalized = String(key || '').toLowerCase();
      const raw = String(value ?? '');
      const sensitive = normalized.includes('token') || normalized.includes('secret') || normalized.includes('password') || normalized.includes('key');
      if (!sensitive) return raw;
      if (raw.length <= 8) return '***';
      return `${raw.slice(0, 4)}...${raw.slice(-4)}`;
    }
    function renderChannelConfig(config) {
      const obj = config && typeof config === 'object' ? config : {};
      const keys = Object.keys(obj);
      if (!keys.length) return '<div class="muted">no config</div>';
      return keys
        .sort()
        .map((key) => `<div class="channel-row"><div class="channel-key">${esc(key)}</div><div class="channel-val">${esc(maskChannelConfigValue(key, obj[key]))}</div></div>`)
        .join('');
    }
    function renderChannels(channels) {
      if (!channels.length) return '<div class="muted">empty</div>';
      return `<div class="channel-list">` + channels.map((c) => {
        const id = String(c.id || '');
        const type = String(c.type || 'unknown');
        const enabled = c.enabled === false ? false : true;
        return `
          <div class="channel-item">
            <div class="channel-head">
              <div>
                <div class="channel-title">${esc(id)}</div>
                <div class="channel-sub">
                  <span>type: ${esc(type)}</span>
                  <span class="channel-tag ${enabled ? 'on' : 'off'}">${enabled ? 'enabled' : 'disabled'}</span>
                </div>
              </div>
              <div class="btns">
                <button class="alt" onclick='selectChannelById(${JSON.stringify(id)})'>Edit</button>
                <button class="warn" onclick='removeSelectedChannel(${JSON.stringify(id)})'>Remove</button>
              </div>
            </div>
            <div class="channel-config">${renderChannelConfig(c.config)}</div>
          </div>
        `;
      }).join('') + `</div>`;
    }
    function setResult(ok, msg) {
      const box = document.getElementById('opResult');
      box.className = ok ? 'ok' : 'warn';
      box.textContent = msg;
      setTimeout(() => { box.textContent = ''; }, 6000);
    }
    let serverReadOnly = false;
    let tasksCache = [];
    let channelsCache = [];
    let todayTasksCache = [];
    let taskFormDirty = false;
    let channelFormDirty = false;
    let todayFormDirty = false;
    const channelFormFieldIds = [
      'channelFormId','channelFormType','channelFormEnabled','channelFormBotToken',
      'channelFormChatId','channelFormUrl','channelFormSecret'
    ];
    const todayFormFieldIds = ['todayFormName', 'todayFormStatus', 'todayFormTime'];
    const taskFormFieldIds = [
      'taskFormName','taskFormTaskKind','taskFormCycleType','taskFormTime','taskFormStartDate',
      'taskFormEndDate','taskFormDeliveryTarget','taskFormIsActive','taskFormWeekday','taskFormIntervalHours',
      'taskFormDatesList','taskFormNPerMonth','taskFormRangeStart','taskFormRangeEnd'
    ];
    const taskPatchKeys = [
      'name','category','cycle_type','weekday','day_of_month','range_start','range_end',
      'n_per_month','interval_hours','time_of_day','end_date','start_date','reminder_template',
      'task_kind','source','legacy_entry_id','special_handler','handler_payload',
      'delivery_target','delivery_mode','dates_list','is_active'
    ];
    function editingEnabled() {
      return document.getElementById('editMode').checked;
    }
    function ensureWritableAction() {
      if (serverReadOnly) throw new Error('server is running in read-only mode');
      if (!editingEnabled()) throw new Error('enable edit mode first');
    }
    function applyEditLock() {
      const enabled = editingEnabled() && !serverReadOnly;
      const targets = [
        'legacyChatId','editChannelSelect','channelFormId','channelFormType','channelFormEnabled',
        'channelFormBotToken','channelFormChatId','channelFormUrl','channelFormSecret','todayTaskSelect',
        'todayFormName','todayFormStatus','todayFormTime','editTaskSelect',
        'taskFormName','taskFormTaskKind','taskFormCycleType','taskFormTime','taskFormStartDate',
        'taskFormEndDate','taskFormDeliveryTarget','taskFormIsActive','taskFormWeekday','taskFormIntervalHours',
        'taskFormDatesList','taskFormNPerMonth','taskFormRangeStart','taskFormRangeEnd'
      ];
      for (const id of targets) {
        const el = document.getElementById(id);
        if (el) el.disabled = !enabled;
      }
      document.querySelectorAll('button').forEach((btn) => {
        const text = (btn.textContent || '').toLowerCase();
        if (text.includes('create') || text.includes('update') || text.includes('save') || text.includes('remove') || text.includes('delete') || text.includes('deactivate') || text.includes('upsert')) {
          btn.disabled = !enabled;
        }
      });
      const state = document.getElementById('editState');
      if (serverReadOnly) {
        state.textContent = 'Server mode: read-only';
      } else if (enabled) {
        state.textContent = 'Server mode: writable; browser edit mode enabled';
      } else {
        state.textContent = 'Server mode: writable; browser edit mode disabled';
      }
      syncChannelFormVisibility();
      syncTaskCycleFormVisibility();
    }
    document.addEventListener('DOMContentLoaded', () => {
      const em = document.getElementById('editMode');
      em.addEventListener('change', applyEditLock);
      const channelSelect = document.getElementById('editChannelSelect');
      if (channelSelect) channelSelect.addEventListener('change', () => loadSelectedChannel(false));
      const todayTaskSelect = document.getElementById('todayTaskSelect');
      if (todayTaskSelect) todayTaskSelect.addEventListener('change', () => loadSelectedTodayTask(false));
      const taskSelect = document.getElementById('editTaskSelect');
      if (taskSelect) taskSelect.addEventListener('change', () => loadSelectedTask(false));
      const taskCycleType = document.getElementById('taskFormCycleType');
      if (taskCycleType) taskCycleType.addEventListener('change', () => {
        taskFormDirty = true;
        syncTaskCycleFormVisibility();
      });
      const channelType = document.getElementById('channelFormType');
      if (channelType) channelType.addEventListener('change', () => {
        channelFormDirty = true;
        syncChannelFormVisibility();
      });
      for (const id of channelFormFieldIds) {
        const el = document.getElementById(id);
        if (!el) continue;
        el.addEventListener('input', () => { channelFormDirty = true; });
        el.addEventListener('change', () => { channelFormDirty = true; });
      }
      for (const id of todayFormFieldIds) {
        const el = document.getElementById(id);
        if (!el) continue;
        el.addEventListener('input', () => { todayFormDirty = true; });
        el.addEventListener('change', () => { todayFormDirty = true; });
      }
      for (const id of taskFormFieldIds) {
        const el = document.getElementById(id);
        if (!el) continue;
        el.addEventListener('input', () => { taskFormDirty = true; });
        el.addEventListener('change', () => { taskFormDirty = true; });
      }
      syncChannelFormVisibility();
      applyEditLock();
    });
    async function callApi(path, payload) {
      const res = await fetch(path, {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload || {})
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'request failed');
      return data;
    }
    function setChannelFormValue(id, value) {
      const el = document.getElementById(id);
      if (!el) return;
      el.value = value == null ? '' : String(value);
    }
    function syncChannelFormVisibility() {
      const channelType = String(document.getElementById('channelFormType').value || 'telegram');
      const isTelegram = channelType === 'telegram';
      const writable = editingEnabled() && !serverReadOnly;
      const tgIds = ['channelFormBotToken', 'channelFormChatId'];
      const whIds = ['channelFormUrl', 'channelFormSecret'];
      for (const id of tgIds) {
        const el = document.getElementById(id);
        if (el) el.disabled = !writable || (!isTelegram && channelType === 'webhook');
      }
      for (const id of whIds) {
        const el = document.getElementById(id);
        if (el) el.disabled = !writable || isTelegram;
      }
    }
    function renderChannelOptions(backgroundRefresh = false) {
      const select = document.getElementById('editChannelSelect');
      if (!select) return;
      const previous = select.value;
      const items = channelsCache || [];
      select.innerHTML = '';
      const createOpt = document.createElement('option');
      createOpt.value = '';
      createOpt.textContent = 'Create new channel...';
      select.appendChild(createOpt);
      for (const channel of items) {
        const opt = document.createElement('option');
        const id = String(channel.id || '');
        const type = String(channel.type || 'unknown');
        const enabled = channel.enabled === false ? 'disabled' : 'enabled';
        opt.value = id;
        opt.textContent = `${id} (${type}, ${enabled})`;
        select.appendChild(opt);
      }
      if (!items.length) {
        startCreateChannel(false);
        return;
      }
      const matched = items.some(c => String(c.id || '') === previous);
      if (previous === '') {
        select.value = '';
      } else {
        select.value = matched ? previous : String(items[0].id || '');
      }
      if (backgroundRefresh && channelFormDirty) return;
      loadSelectedChannel(false);
    }
    function fillChannelForm(channel) {
      const config = channel && typeof channel.config === 'object' && channel.config ? channel.config : {};
      setChannelFormValue('channelFormId', channel.id || '');
      setChannelFormValue('channelFormType', channel.type || 'telegram');
      setChannelFormValue('channelFormEnabled', channel.enabled === false ? '0' : '1');
      setChannelFormValue('channelFormBotToken', config.bot_token || '');
      setChannelFormValue('channelFormChatId', config.chat_id || '');
      setChannelFormValue('channelFormUrl', config.url || '');
      setChannelFormValue('channelFormSecret', config.secret || '');
      syncChannelFormVisibility();
    }
    function startCreateChannel(showMessage = true) {
      const select = document.getElementById('editChannelSelect');
      if (select) select.value = '';
      fillChannelForm({
        id: '',
        type: 'telegram',
        enabled: true,
        config: {}
      });
      channelFormDirty = false;
      if (showMessage) setResult(true, 'switched to channel create mode');
    }
    function loadSelectedChannel(showMessage = true) {
      const select = document.getElementById('editChannelSelect');
      const id = String((select && select.value) || '').trim();
      if (!id) {
        startCreateChannel(false);
        if (showMessage) setResult(true, 'channel create mode');
        return;
      }
      const channel = (channelsCache || []).find(c => String(c.id || '') === id);
      if (!channel) {
        if (showMessage) setResult(false, `channel ${id} not found in current snapshot`);
        return;
      }
      fillChannelForm(channel);
      channelFormDirty = false;
      if (showMessage) setResult(true, `loaded channel ${id} for editing`);
    }
    function selectChannelById(channelId, showMessage = true) {
      const id = String(channelId || '').trim();
      const select = document.getElementById('editChannelSelect');
      if (select) select.value = id;
      loadSelectedChannel(showMessage);
    }
    function buildChannelFromForm() {
      const id = String(document.getElementById('channelFormId').value || '').trim();
      const type = String(document.getElementById('channelFormType').value || '').trim();
      const enabled = String(document.getElementById('channelFormEnabled').value || '1') === '1';
      if (!id) throw new Error('channel id is required');
      if (!type) throw new Error('channel type is required');
      const botToken = String(document.getElementById('channelFormBotToken').value || '').trim();
      const chatId = String(document.getElementById('channelFormChatId').value || '').trim();
      const url = String(document.getElementById('channelFormUrl').value || '').trim();
      const secret = String(document.getElementById('channelFormSecret').value || '').trim();
      const config = {};
      if (type === 'telegram') {
        if (!botToken) throw new Error('telegram bot_token is required');
        if (!chatId) throw new Error('telegram chat_id is required');
        config.bot_token = botToken;
        config.chat_id = chatId;
      } else if (type === 'webhook') {
        if (!url) throw new Error('webhook url is required');
        config.url = url;
        if (secret) config.secret = secret;
      } else {
        throw new Error('unsupported channel type');
      }
      return { id, type, enabled, config };
    }
    async function upsertChannelFromForm() {
      try {
        ensureWritableAction();
        const channel = buildChannelFromForm();
        await callApi(`${API_BASE}/channel/put`, { channel });
        setResult(true, `upserted channel ${channel.id}`);
        await load();
      } catch (e) { setResult(false, e.message); }
    }
    async function removeSelectedChannel(channelId = null) {
      try {
        ensureWritableAction();
        const select = document.getElementById('editChannelSelect');
        const id = channelId == null
          ? String((select && select.value) || '').trim()
          : String(channelId).trim();
        if (!id) throw new Error('select a channel first');
        const sure = window.confirm(`Remove channel ${id}?`);
        if (!sure) return;
        await callApi(`${API_BASE}/channel/remove`, { id });
        setResult(true, `removed channel ${id}`);
        await load();
      } catch (e) { setResult(false, e.message); }
    }
    function setTodayFormValue(id, value) {
      const el = document.getElementById(id);
      if (!el) return;
      el.value = value == null ? '' : String(value);
    }
    function fillTodayForm(item) {
      setTodayFormValue('todayTaskIdentifier', item.identifier || '');
      setTodayFormValue('todayTaskType', item.item_type || '');
      setTodayFormValue('todayFormName', item.name || '');
      setTodayFormValue('todayFormStatus', item.status || 'pending');
      setTodayFormValue('todayFormTime', item.scheduled_time || '');
    }
    function renderTodayOptions(backgroundRefresh = false) {
      const select = document.getElementById('todayTaskSelect');
      if (!select) return;
      const previous = select.value;
      const items = todayTasksCache || [];
      select.innerHTML = '';
      if (!items.length) {
        setTodayFormValue('todayTaskIdentifier', '');
        setTodayFormValue('todayTaskType', '');
        setTodayFormValue('todayFormName', '');
        setTodayFormValue('todayFormStatus', 'pending');
        setTodayFormValue('todayFormTime', '');
        return;
      }
      for (const item of items) {
        const opt = document.createElement('option');
        const identifier = String(item.identifier || '');
        const kind = String(item.item_type || '');
        opt.value = identifier;
        opt.textContent = `${identifier} ${item.name || ''} (${kind}, ${item.status || ''})`;
        select.appendChild(opt);
      }
      const matched = items.some(t => String(t.identifier || '') === previous);
      select.value = matched ? previous : String(items[0].identifier || '');
      if (backgroundRefresh && todayFormDirty) return;
      loadSelectedTodayTask(false);
    }
    function loadSelectedTodayTask(showMessage = true) {
      const select = document.getElementById('todayTaskSelect');
      const identifier = String((select && select.value) || '').trim();
      if (!identifier) {
        if (showMessage) setResult(false, 'select a today task first');
        return;
      }
      const item = (todayTasksCache || []).find(t => String(t.identifier || '') === identifier);
      if (!item) {
        if (showMessage) setResult(false, `today task ${identifier} not found in current snapshot`);
        return;
      }
      fillTodayForm(item);
      todayFormDirty = false;
      if (showMessage) setResult(true, `loaded ${identifier} for editing`);
    }
    function selectTodayByIdentifier(identifier, showMessage = true) {
      const id = String(identifier || '').trim();
      const select = document.getElementById('todayTaskSelect');
      if (select) select.value = id;
      loadSelectedTodayTask(showMessage);
    }
    function buildTodayPatchFromForm(item) {
      const name = String(document.getElementById('todayFormName').value || '').trim();
      const status = String(document.getElementById('todayFormStatus').value || '').trim();
      const scheduledTime = String(document.getElementById('todayFormTime').value || '').trim();
      if (!name) throw new Error('name is required');
      if (!status) throw new Error('status is required');
      const patch = { name, status };
      if (item.item_type === 'occurrence') {
        patch.scheduled_time = scheduledTime || null;
      }
      return patch;
    }
    async function updateSelectedTodayTask() {
      try {
        ensureWritableAction();
        const select = document.getElementById('todayTaskSelect');
        const identifier = String((select && select.value) || '').trim();
        if (!identifier) throw new Error('select a today task first');
        const item = (todayTasksCache || []).find(t => String(t.identifier || '') === identifier);
        if (!item) throw new Error(`today task ${identifier} not found in current snapshot`);
        const patch = buildTodayPatchFromForm(item);
        await callApi(`${API_BASE}/today/update`, { identifier, patch });
        setResult(true, `updated ${identifier}`);
        await load();
      } catch (e) { setResult(false, e.message); }
    }
    async function removeSelectedTodayTask(identifier = null) {
      try {
        ensureWritableAction();
        const select = document.getElementById('todayTaskSelect');
        const picked = identifier == null
          ? String((select && select.value) || '').trim()
          : String(identifier).trim();
        if (!picked) throw new Error('select a today task first');
        const sure = window.confirm(`Delete today task ${picked}?`);
        if (!sure) return;
        await callApi(`${API_BASE}/today/remove`, { identifier: picked });
        setResult(true, `deleted ${picked}`);
        await load();
      } catch (e) { setResult(false, e.message); }
    }
    async function createTaskFromForm() {
      try {
        ensureWritableAction();
        const payload = buildPatchFromForm();
        const r = await callApi(`${API_BASE}/task/create`, { payload });
        setResult(true, `created task ${r.data.id}`);
        await load();
      } catch (e) { setResult(false, e.message); }
    }
    function renderTaskOptions(backgroundRefresh = false) {
      const select = document.getElementById('editTaskSelect');
      if (!select) return;
      const previous = select.value;
      const items = tasksCache || [];
      select.innerHTML = '';
      const createOpt = document.createElement('option');
      createOpt.value = '';
      createOpt.textContent = 'Create new task...';
      select.appendChild(createOpt);
      if (!items.length) {
        document.getElementById('originalTaskJson').value = '{}';
        startCreateTask(false);
        return;
      }
      for (const task of items) {
        const opt = document.createElement('option');
        opt.value = String(task.id ?? '');
        const cycle = task.cycle_type || 'once';
        const t = task.time_of_day || '';
        const active = String(task.is_active) === '1' ? 'active' : 'inactive';
        opt.textContent = `#${task.id} ${task.name || ''} (${cycle}${t ? ' ' + t : ''}, ${active})`;
        select.appendChild(opt);
      }
      const matched = items.some(t => String(t.id ?? '') === previous);
      if (previous === '') {
        select.value = '';
      } else {
        select.value = matched ? previous : String(items[0].id ?? '');
      }
      if (backgroundRefresh && taskFormDirty) return;
      loadSelectedTask(false);
    }
    function buildEditablePatch(task) {
      const patch = {};
      for (const key of taskPatchKeys) {
        if (!(key in task)) continue;
        patch[key] = task[key];
      }
      return patch;
    }
    function startCreateTask(showMessage = true) {
      const select = document.getElementById('editTaskSelect');
      if (select) select.value = '';
      document.getElementById('originalTaskJson').value = '{}';
      fillTaskForm({
        name: '',
        task_kind: 'scheduled',
        cycle_type: 'once',
        time_of_day: '',
        start_date: '',
        end_date: '',
        delivery_target: '',
        is_active: 1,
        weekday: null,
        interval_hours: null,
        dates_list: '',
        n_per_month: null,
        range_start: null,
        range_end: null
      });
      taskFormDirty = false;
      if (showMessage) setResult(true, 'switched to create mode');
    }
    function loadSelectedTask(showMessage = true) {
      const select = document.getElementById('editTaskSelect');
      const id = Number((select && select.value) || 0);
      if (!id) {
        startCreateTask(false);
        if (showMessage) setResult(true, 'create mode');
        return;
      }
      const task = (tasksCache || []).find(t => Number(t.id) === id);
      if (!task) {
        if (showMessage) setResult(false, `task ${id} not found in current snapshot`);
        return;
      }
      document.getElementById('originalTaskJson').value = JSON.stringify(task, null, 2);
      const patch = buildEditablePatch(task);
      fillTaskForm(patch);
      taskFormDirty = false;
      if (showMessage) setResult(true, `loaded task ${id} for editing`);
    }
    function setFormValue(id, value) {
      const el = document.getElementById(id);
      if (!el) return;
      el.value = value == null ? '' : String(value);
    }
    function fillTaskForm(task) {
      let uiCycle = task.cycle_type || 'once';
      let uiDatesList = task.dates_list || '';
      let uiRangeStart = task.range_start;
      let uiRangeEnd = task.range_end;
      if (uiCycle === 'monthly_fixed') {
        uiCycle = 'monthly_dates';
        if (!uiDatesList && task.day_of_month != null) uiDatesList = String(task.day_of_month);
      } else if (uiCycle === 'monthly_n_times') {
        uiCycle = 'monthly_range';
        if (uiRangeStart == null) uiRangeStart = 1;
        if (uiRangeEnd == null) uiRangeEnd = 31;
      }
      setFormValue('taskFormName', task.name || '');
      setFormValue('taskFormTaskKind', task.task_kind || 'scheduled');
      setFormValue('taskFormCycleType', uiCycle);
      setFormValue('taskFormTime', task.time_of_day || '');
      setFormValue('taskFormStartDate', task.start_date || '');
      setFormValue('taskFormEndDate', task.end_date || '');
      setFormValue('taskFormDeliveryTarget', task.delivery_target || '');
      setFormValue('taskFormIsActive', String(task.is_active) === '0' ? '0' : '1');
      setFormValue('taskFormWeekday', task.weekday);
      setFormValue('taskFormIntervalHours', task.interval_hours);
      setFormValue('taskFormDatesList', uiDatesList);
      setFormValue('taskFormNPerMonth', task.n_per_month);
      setFormValue('taskFormRangeStart', uiRangeStart);
      setFormValue('taskFormRangeEnd', uiRangeEnd);
      syncTaskCycleFormVisibility();
    }
    function parseOptionalInt(id, min, max, fieldName) {
      const raw = String(document.getElementById(id).value || '').trim();
      if (!raw) return null;
      const num = Number(raw);
      if (!Number.isInteger(num)) throw new Error(`${fieldName} must be integer`);
      if (num < min || num > max) throw new Error(`${fieldName} must be ${min}-${max}`);
      return num;
    }
    function syncTaskCycleFormVisibility() {
      const cycle = String(document.getElementById('taskFormCycleType').value || '');
      const writable = editingEnabled() && !serverReadOnly;
      const dateIds = ['taskFormDatesList'];
      const rangeIds = ['taskFormRangeStart', 'taskFormRangeEnd', 'taskFormNPerMonth'];
      for (const id of dateIds) {
        const el = document.getElementById(id);
        if (el) el.disabled = !writable || cycle !== 'monthly_dates';
      }
      for (const id of rangeIds) {
        const el = document.getElementById(id);
        if (el) el.disabled = !writable || cycle !== 'monthly_range';
      }
    }
    function buildPatchFromForm() {
      const name = String(document.getElementById('taskFormName').value || '').trim();
      if (!name) throw new Error('task name is required');
      const cycle_type = String(document.getElementById('taskFormCycleType').value || '').trim();
      const task_kind = String(document.getElementById('taskFormTaskKind').value || '').trim();
      const time_of_day = String(document.getElementById('taskFormTime').value || '').trim();
      const start_date = String(document.getElementById('taskFormStartDate').value || '').trim();
      const end_date = String(document.getElementById('taskFormEndDate').value || '').trim();
      const delivery_target = String(document.getElementById('taskFormDeliveryTarget').value || '').trim();
      const is_active = String(document.getElementById('taskFormIsActive').value || '1') === '1';
      const weekday = parseOptionalInt('taskFormWeekday', 0, 6, 'weekday');
      const interval_hours = parseOptionalInt('taskFormIntervalHours', 1, 24, 'interval_hours');
      const n_per_month = parseOptionalInt('taskFormNPerMonth', 1, 365, 'n_per_month');
      const range_start = parseOptionalInt('taskFormRangeStart', 1, 31, 'range_start');
      const range_end = parseOptionalInt('taskFormRangeEnd', 1, 31, 'range_end');
      const dates_list = String(document.getElementById('taskFormDatesList').value || '').trim();
      const patch = {
        name,
        cycle_type,
        task_kind,
        is_active,
        start_date: start_date || null,
        end_date: end_date || null,
        delivery_target: delivery_target || null,
        weekday,
        interval_hours,
        n_per_month,
        range_start,
        range_end,
        dates_list: dates_list || null
      };
      if (cycle_type === 'monthly_dates' && !patch.dates_list) throw new Error('monthly_dates requires dates_list');
      if (cycle_type === 'monthly_range' && (patch.range_start == null || patch.range_end == null)) {
        throw new Error('monthly_range requires range_start and range_end');
      }
      if (time_of_day) patch.time_of_day = time_of_day;
      return patch;
    }
    async function updateSelectedTaskByForm() {
      try {
        ensureWritableAction();
        const select = document.getElementById('editTaskSelect');
        const id = Number((select && select.value) || 0);
        if (!id) throw new Error('select a task first');
        const patch = buildPatchFromForm();
        const r = await callApi(`${API_BASE}/task/update`, { id, patch });
        setResult(true, `updated task ${r.data.id}`);
        await load();
      } catch (e) { setResult(false, e.message); }
    }
    function selectTaskById(taskId, showMessage = true) {
      const id = Number(taskId || 0);
      const select = document.getElementById('editTaskSelect');
      if (select) select.value = id > 0 ? String(id) : '';
      loadSelectedTask(showMessage);
      if (id > 0) {
        const taskOpsSection = document.getElementById('editTaskSelect');
        if (taskOpsSection && typeof taskOpsSection.scrollIntoView === 'function') {
          taskOpsSection.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      }
    }
    async function removeSelectedTask(hard, taskId = null) {
      try {
        ensureWritableAction();
        const select = document.getElementById('editTaskSelect');
        const id = taskId == null
          ? Number((select && select.value) || 0)
          : Number(taskId || 0);
        if (!id) throw new Error('select a task first');
        if (hard) {
          const sure = window.prompt(`Type DELETE-${id} to confirm hard deletion`);
          if (sure !== `DELETE-${id}`) throw new Error('hard delete cancelled');
        }
        await callApi(`${API_BASE}/task/remove`, { id, hard });
        setResult(true, hard ? `hard-deleted task ${id}` : `deactivated task ${id}`);
        await load();
      } catch (e) { setResult(false, e.message); }
    }
    async function updateSettings() {
      try {
        ensureWritableAction();
        const chat_id = document.getElementById('legacyChatId').value.trim();
        await callApi(`${API_BASE}/settings/update`, { chat_id });
        setResult(true, 'updated settings');
        await load();
      } catch (e) { setResult(false, e.message); }
    }
    async function load(backgroundRefresh = false) {
      const res = await fetch(`${API_BASE}/snapshot`);
      const data = await res.json();
      document.getElementById('meta').textContent = `updated: ${data.generated_at} | today: ${data.today}`;
      const s = data.settings || {};
      serverReadOnly = Boolean(s.read_only);
      document.getElementById('settings').innerHTML = `
        <div><span class="pill">${esc(s.config_status || 'unknown')}</span></div>
        <pre>config_path: ${esc(s.config_path)}
config_exists: ${esc(s.config_exists)}
channels_present: ${esc(s.channels_present)}
system_scheduler: ${esc(s.system_scheduler)}
read_only: ${esc(s.read_only)}
db_path: ${esc(s.db_path)}</pre>
        ${s.error ? `<div class="warn">error: ${esc(s.error)}</div>` : ''}
      `;
      applyEditLock();
      if (data.legacy_chat_id) {
        document.getElementById('legacyChatId').value = data.legacy_chat_id;
      }
      const channels = data.channels || [];
      channelsCache = channels;
      renderChannelOptions(backgroundRefresh);
      document.getElementById('channels').innerHTML = renderChannels(channels);
      const today = data.today_tasks || [];
      todayTasksCache = today;
      renderTodayOptions(backgroundRefresh);
      document.getElementById('today').innerHTML = table(
        ['id','name','kind','cycle','time','status','source','actions'],
        today.map(t => [
          esc(t.identifier),
          esc(t.name),
          esc(t.task_kind),
          esc(t.cycle_type),
          esc(t.scheduled_time || ''),
          esc(t.status),
          esc(t.source || ''),
          `<div class="btns"><button class="alt" onclick='selectTodayByIdentifier(${JSON.stringify(String(t.identifier || ""))})'>Edit</button><button class="warn" onclick='removeSelectedTodayTask(${JSON.stringify(String(t.identifier || ""))})'>Delete</button></div>`
        ])
      );
      const tasks = data.tasks || [];
      tasksCache = tasks;
      renderTaskOptions(backgroundRefresh);
      document.getElementById('tasks').innerHTML = table(
        ['id','name','active','kind','cycle','time','delivery_target','source','actions'],
        tasks.map(t => [
          esc(t.id),
          esc(t.name),
          esc(t.is_active),
          esc(t.task_kind || 'scheduled'),
          esc(t.cycle_type),
          esc(t.time_of_day || ''),
          esc(t.delivery_target || ''),
          esc(t.source || ''),
          `<div class="btns"><button class="alt" onclick='selectTaskById(${JSON.stringify(Number(t.id || 0))})'>Edit</button><button class="warn" onclick='removeSelectedTask(false, ${JSON.stringify(Number(t.id || 0))})'>Deactivate</button><button class="warn" onclick='removeSelectedTask(true, ${JSON.stringify(Number(t.id || 0))})'>Delete</button></div>`
        ])
      );
    }
    load();
    setInterval(() => load(true), 30000);
  </script>
</body>
</html>
"""


def _safe_query(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict]:
    try:
        cur = conn.execute(query, params)
        columns = [item[0] for item in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    except sqlite3.Error:
        return []


def build_snapshot(db_path: Path, *, read_only: bool = False) -> dict:
    now = datetime.now(SHANGHAI_TZ)
    today = now.date().isoformat()
    config_info = inspect_config()

    snapshot = {
        "generated_at": now.isoformat(timespec="seconds"),
        "today": today,
        "legacy_chat_id": None,
        "settings": {
            "config_status": config_info.get("status"),
            "config_path": config_info.get("config_path"),
            "config_exists": config_info.get("config_exists"),
            "channels_present": config_info.get("channels_present"),
            "system_scheduler": supports_system_scheduler(),
            "read_only": bool(read_only),
            "db_path": str(db_path),
            "error": config_info.get("error"),
        },
        "channels": [],
        "tasks": [],
        "today_tasks": [],
    }

    raw_config = config_info.get("config") if isinstance(config_info.get("config"), dict) else {}
    legacy_chat_id = raw_config.get("chat_id")
    if legacy_chat_id is not None:
        snapshot["legacy_chat_id"] = str(legacy_chat_id)
    channels = raw_config.get("channels")
    if isinstance(channels, list):
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            snapshot["channels"].append(
                {
                    "id": channel.get("id"),
                    "type": channel.get("type"),
                    "enabled": False if channel.get("enabled", True) is False else True,
                    "config": channel.get("config") if isinstance(channel.get("config"), dict) else channel,
                }
            )

    if not db_path.exists():
        return snapshot

    conn = sqlite3.connect(str(db_path))
    try:
        tasks = _safe_query(
            conn,
            """
            SELECT
              id, name, category, cycle_type, weekday, day_of_month, range_start, range_end,
              n_per_month, interval_hours, time_of_day, end_date, start_date, reminder_template,
              task_kind, source, legacy_entry_id, special_handler, handler_payload,
              delivery_target, delivery_mode, dates_list, is_active
            FROM periodic_tasks
            ORDER BY is_active DESC, id DESC
            """,
        )
        snapshot["tasks"] = tasks

        today_rows = _safe_query(
            conn,
            """
            SELECT
              o.id AS occ_id,
              t.id AS task_id,
              t.name,
              t.cycle_type,
              COALESCE(t.task_kind, 'scheduled') AS task_kind,
              COALESCE(o.scheduled_time, t.time_of_day) AS scheduled_time,
              o.status,
              t.source
            FROM periodic_occurrences o
            JOIN periodic_tasks t ON t.id = o.task_id
            WHERE o.date = ?
            ORDER BY COALESCE(o.scheduled_time, t.time_of_day), t.name, o.id
            """,
            (today,),
        )
        today_tasks = []
        for row in today_rows:
            row["identifier"] = f"FIN-{row.get('occ_id')}"
            row["item_type"] = "occurrence"
            row["occurrence_id"] = row.get("occ_id")
            row["entry_id"] = None
            today_tasks.append(row)

        simple_rows = _safe_query(
            conn,
            """
            SELECT e.id, e.text, e.status, COALESCE(g.name, 'Inbox') AS group_name
            FROM entries e
            LEFT JOIN groups g ON g.id = e.group_id
            WHERE e.status IN ('pending', 'in_progress')
              AND NOT EXISTS (
                  SELECT 1 FROM periodic_tasks t
                  WHERE t.legacy_entry_id = e.id
              )
            ORDER BY e.id
            """,
        )
        for row in simple_rows:
            today_tasks.append(
                {
                    "identifier": f"ID{row.get('id')}",
                    "item_type": "entry",
                    "occurrence_id": None,
                    "entry_id": row.get("id"),
                    "name": row.get("text"),
                    "cycle_type": "legacy",
                    "task_kind": "entry",
                    "scheduled_time": "",
                    "status": row.get("status"),
                    "source": row.get("group_name"),
                }
            )
        snapshot["today_tasks"] = today_tasks
    finally:
        conn.close()

    return snapshot


def build_health(db_path: Path, *, read_only: bool = False) -> dict:
    now = datetime.now(SHANGHAI_TZ)
    db_ok = False
    db_error = None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("SELECT 1")
        conn.close()
        db_ok = True
    except sqlite3.Error as exc:
        db_error = str(exc)

    status = "ok" if db_ok else "degraded"
    return {
        "status": status,
        "generated_at": now.isoformat(timespec="seconds"),
        "db_ok": db_ok,
        "db_path": str(db_path),
        "db_error": db_error,
        "system_scheduler": supports_system_scheduler(),
        "read_only": bool(read_only),
        "metrics": METRICS.snapshot(),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    db_path: Path = TODO_DB
    basic_auth_token: str | None = None
    debug_errors: bool = False
    read_only_mode: bool = False

    def do_GET(self) -> None:  # noqa: N802
        if not self._check_auth():
            METRICS.inc("web_auth_rejected_total")
            self._write_auth_required()
            return
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._write_html(HTML_PAGE)
            return
        normalized_path = _normalize_api_path(parsed.path)
        if normalized_path == "/api/v1/snapshot":
            METRICS.inc("web_snapshot_requests_total")
            payload = build_snapshot(self.db_path, read_only=self.read_only_mode)
            self._write_json(payload)
            return
        if normalized_path == "/api/v1/health":
            METRICS.inc("web_health_requests_total")
            payload = build_health(self.db_path, read_only=self.read_only_mode)
            self._write_json(payload)
            return
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"not found")

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_auth():
            METRICS.inc("web_auth_rejected_total")
            self._write_auth_required()
            return
        parsed = urlparse(self.path)
        try:
            if self.read_only_mode:
                raise ValueError("server is running in read-only mode")
            payload = self._read_json_body()
            result = handle_mutation(parsed.path, payload, db_path=self.db_path)
            METRICS.inc("web_mutation_success_total")
            self._write_json({"ok": True, "data": result})
        except ValueError as exc:
            METRICS.inc("web_mutation_client_error_total")
            emit_log("web.mutation.client_error", level="WARNING", path=parsed.path, error=str(exc))
            self._write_json({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            METRICS.inc("web_mutation_server_error_total")
            if self.debug_errors:
                self._write_json({"ok": False, "error": str(exc)}, status=500)
            else:
                emit_log("web.mutation.server_error", level="ERROR", path=parsed.path, error=str(exc))
                print(f"[web_dashboard] internal error on {parsed.path}: {exc}", file=sys.stderr)
                traceback.print_exc()
                self._write_json({"ok": False, "error": "operation failed"}, status=500)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _write_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write_json(self, payload: dict, *, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write_auth_required(self) -> None:
        body = b"Authentication required"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Chronos Dashboard"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        expected = self.basic_auth_token
        if not expected:
            return True
        actual = self.headers.get("Authorization") or ""
        return hmac.compare_digest(actual, expected)

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length)
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data


def _normalize_api_path(path: str) -> str:
    clean = (path or "").strip()
    if clean.startswith("/api/v1/"):
        return clean
    if clean.startswith("/api/"):
        return "/api/v1/" + clean[len("/api/") :]
    return clean


def _expect_object(value: Any, *, field_name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    return value


def _parse_int_field(payload: dict, key: str, *, minimum: int | None = None) -> int:
    value = payload.get(key)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    return parsed


def _parse_bool_field(payload: dict, key: str, *, default: bool = False) -> bool:
    if key not in payload:
        return default
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    raise ValueError(f"{key} must be a boolean")


def _parse_channel_id(payload: dict) -> str:
    channel_id = str(payload.get("id") or "").strip()
    if not channel_id:
        raise ValueError("id is required")
    return channel_id


def _db_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    return conn


def _parse_today_identifier(identifier: str) -> tuple[str, int]:
    raw = (identifier or "").strip()
    if raw.startswith("FIN-"):
        token = raw[4:].strip()
        if not token.isdigit():
            raise ValueError("invalid today identifier")
        return "occurrence", int(token)
    if raw.startswith("ID"):
        token = raw[2:].strip()
        if not token.isdigit():
            raise ValueError("invalid today identifier")
        return "entry", int(token)
    raise ValueError("invalid today identifier")


def _validate_time_of_day(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not _TIME_OF_DAY_RE.match(text):
        raise ValueError("scheduled_time must be HH:MM")
    return text


def _validate_today_status(value: Any) -> str:
    status = str(value or "").strip()
    allowed = {"pending", "in_progress", "reminded", "completed", "skipped"}
    if status not in allowed:
        raise ValueError("status must be one of pending/in_progress/reminded/completed/skipped")
    return status


def _remove_scheduler_jobs_for_payload(payload: dict[str, Any]) -> None:
    if not supports_system_scheduler():
        return
    for _kind, job_name in iter_job_refs(payload):
        remove_job(job_name)


def _remove_scheduler_jobs_for_pair(reminder_job_id: Any, execution_job_id: Any) -> None:
    if not supports_system_scheduler():
        return
    for _kind, job_name in iter_job_refs_from_pair(reminder_job_id, execution_job_id):
        remove_job(job_name)


def update_today_task(db_path: Path, *, identifier: str, patch: dict) -> dict:
    kind, row_id = _parse_today_identifier(identifier)
    patch = _expect_object(patch, field_name="patch")
    name = str(patch.get("name") or "").strip()
    if not name:
        raise ValueError("patch.name is required")
    status = _validate_today_status(patch.get("status"))
    scheduled_time = _validate_time_of_day(patch.get("scheduled_time"))
    conn = _db_connect(db_path)
    try:
        if kind == "occurrence":
            occurrence_columns = {info[1] for info in conn.execute("PRAGMA table_info(periodic_occurrences)").fetchall()}
            execution_expr = "o.execution_job_id" if "execution_job_id" in occurrence_columns else "NULL AS execution_job_id"
            row = conn.execute(
                f"""
                SELECT o.id, o.task_id, o.reminder_job_id, {execution_expr}
                FROM periodic_occurrences o
                WHERE o.id = ?
                """,
                (row_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"today task {identifier} not found")
            conn.execute(
                "UPDATE periodic_tasks SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (name, int(row["task_id"])),
            )
            state_store = OccurrenceStateStore(conn)
            if status == "completed":
                changed = state_store.complete(
                    row_id,
                    completion_mode="manual",
                    completion_source="web_dashboard",
                    trigger_label="web_today_update",
                    trigger_command="web_dashboard today update",
                    commit=False,
                )
                if changed:
                    _remove_scheduler_jobs_for_pair(row["reminder_job_id"], row["execution_job_id"])
            elif status == "skipped":
                changed = state_store.skip(
                    row_id,
                    completion_mode="manual",
                    completion_source="web_dashboard",
                    trigger_label="web_today_update",
                    trigger_command="web_dashboard today update",
                    commit=False,
                )
                if changed:
                    _remove_scheduler_jobs_for_pair(row["reminder_job_id"], row["execution_job_id"])
            else:
                job_refs = state_store.update_non_terminal(
                    row_id,
                    status=status,
                    scheduled_time=scheduled_time,
                    commit=False,
                )
                _remove_scheduler_jobs_for_pair(job_refs[0], job_refs[1])
            if status in {"completed", "skipped"}:
                conn.execute("UPDATE periodic_occurrences SET scheduled_time = ? WHERE id = ?", (scheduled_time, row_id))
            conn.commit()
            return {"identifier": identifier, "kind": kind, "id": row_id}
        row = conn.execute("SELECT id FROM entries WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise ValueError(f"today task {identifier} not found")
        conn.execute("UPDATE entries SET text = ?, status = ? WHERE id = ?", (name, status, row_id))
        conn.commit()
        return {"identifier": identifier, "kind": kind, "id": row_id}
    finally:
        conn.close()


def remove_today_task(db_path: Path, *, identifier: str) -> dict:
    kind, row_id = _parse_today_identifier(identifier)
    conn = _db_connect(db_path)
    try:
        if kind == "occurrence":
            if conn.execute("SELECT 1 FROM periodic_occurrences WHERE id = ?", (row_id,)).fetchone() is None:
                raise ValueError(f"today task {identifier} not found")
            job_refs = OccurrenceStateStore(conn).clear_jobs(row_id, commit=False)
            _remove_scheduler_jobs_for_pair(job_refs[0], job_refs[1])
            conn.execute("DELETE FROM periodic_occurrences WHERE id = ?", (row_id,))
            conn.commit()
            return {"identifier": identifier, "kind": kind, "id": row_id}
        cur = conn.execute("DELETE FROM entries WHERE id = ?", (row_id,))
        if cur.rowcount <= 0:
            raise ValueError(f"today task {identifier} not found")
        conn.commit()
        return {"identifier": identifier, "kind": kind, "id": row_id}
    finally:
        conn.close()


def handle_mutation(path: str, payload: dict, *, db_path: Path | None = None) -> dict:
    normalized_path = _normalize_api_path(path)
    payload = _expect_object(payload, field_name="request body")
    resolved_db_path = db_path or TODO_DB
    if normalized_path == "/api/v1/task/create":
        task_payload = _expect_object(payload.get("payload"), field_name="payload")
        return create_task(task_payload)
    if normalized_path == "/api/v1/task/update":
        task_id = _parse_int_field(payload, "id", minimum=1)
        patch = _expect_object(payload.get("patch"), field_name="patch")
        return update_task(task_id, patch)
    if normalized_path == "/api/v1/task/remove":
        task_id = _parse_int_field(payload, "id", minimum=1)
        hard = _parse_bool_field(payload, "hard", default=False)
        removed = remove_task(task_id, hard=hard)
        if not removed:
            raise ValueError(f"task {task_id} not found")
        return {"id": task_id, "hard": hard}
    if normalized_path == "/api/v1/channel/put":
        channel = _expect_object(payload.get("channel"), field_name="channel")
        channel_id = str(channel.get("id") or "").strip()
        channel_type = str(channel.get("type") or "").strip()
        if not channel_id:
            raise ValueError("channel.id is required")
        if not channel_type:
            raise ValueError("channel.type is required")
        return put_channel(channel)
    if normalized_path == "/api/v1/channel/remove":
        channel_id = _parse_channel_id(payload)
        removed = delete_channel(channel_id)
        if not removed:
            raise ValueError(f"channel {channel_id} not found")
        return {"id": channel_id}
    if normalized_path == "/api/v1/settings/update":
        chat_id = str(payload.get("chat_id") or "").strip()
        if chat_id:
            update_raw_config(updates={"chat_id": chat_id})
        else:
            update_raw_config(remove_keys=("chat_id",))
        return {"chat_id": chat_id or None}
    if normalized_path == "/api/v1/today/update":
        identifier = str(payload.get("identifier") or "").strip()
        if not identifier:
            raise ValueError("identifier is required")
        patch = _expect_object(payload.get("patch"), field_name="patch")
        return update_today_task(Path(resolved_db_path), identifier=identifier, patch=patch)
    if normalized_path == "/api/v1/today/remove":
        identifier = str(payload.get("identifier") or "").strip()
        if not identifier:
            raise ValueError("identifier is required")
        return remove_today_task(Path(resolved_db_path), identifier=identifier)
    raise ValueError("unsupported endpoint")


def _is_local_bind(host: str) -> bool:
    normalized = (host or "").strip().lower()
    return normalized in {"127.0.0.1", "localhost", "::1"}


def _encode_basic_auth_token(credential: str) -> str:
    raw = credential.encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chronos web dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db-path", default=str(TODO_DB))
    parser.add_argument("--basic-auth", help="HTTP Basic auth credential in user:password format")
    parser.add_argument("--allow-unauthenticated-remote", action="store_true", help="Allow non-local bind without auth (unsafe)")
    parser.add_argument("--read-only", action="store_true", help="Disable all mutation APIs")
    parser.add_argument("--debug-errors", action="store_true", help="Return internal error details in HTTP responses")
    parser.add_argument("--dump-json", action="store_true", help="Print one snapshot JSON and exit")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    db_path = Path(args.db_path).expanduser()
    if args.dump_json:
        print(json.dumps(build_snapshot(db_path, read_only=bool(args.read_only)), ensure_ascii=False, indent=2))
        return 0

    if not _is_local_bind(args.host) and not args.basic_auth and not args.allow_unauthenticated_remote:
        print(
            "Refusing remote bind without authentication. "
            "Set --basic-auth user:password (recommended) "
            "or use --allow-unauthenticated-remote (unsafe).",
            file=sys.stderr,
        )
        return 2

    DashboardHandler.db_path = db_path
    DashboardHandler.debug_errors = bool(args.debug_errors)
    DashboardHandler.basic_auth_token = _encode_basic_auth_token(args.basic_auth) if args.basic_auth else None
    DashboardHandler.read_only_mode = bool(args.read_only)
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    emit_log(
        "web.server.started",
        host=args.host,
        port=int(args.port),
        read_only=bool(args.read_only),
        auth_enabled=bool(args.basic_auth),
        db_path=str(db_path),
    )
    print(f"Chronos dashboard running at http://{args.host}:{args.port}")
    print(f"Using DB: {db_path}")
    if args.basic_auth:
        print("Basic auth: enabled")
    elif not _is_local_bind(args.host):
        print("Warning: running without auth on remote bind (unsafe)")
    if args.read_only:
        print("Mutation APIs: disabled (read-only mode)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
