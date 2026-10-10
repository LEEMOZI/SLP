-- Cherry Studio law-search MCP 本地配置 SQL
-- 执行前请先完全退出 Cherry Studio（Cmd+Q）
-- 执行方式：
--   sqlite3 "/Users/simon/Library/Application Support/CherryStudio/Data/cherrystudio.sqlite" < cherry_studio_setup.sql

INSERT INTO mcp_server (
  id, name, type, description, base_url, command, registry_url, args, env, headers,
  provider, provider_url, logo_url, tags, long_running, timeout,
  dxt_version, dxt_path, reference, search_key, config_sample,
  disabled_tools, disabled_auto_approve_tools, should_config,
  sort_order, is_active, install_source, is_trusted, trusted_at,
  installed_at, created_at, updated_at
) VALUES (
  'law-search-local',
  'law-search',
  'stdio',
  '本地法律检索 MCP — 法律法规检索、合同审查、文书生成、证据评估',
  NULL,
  '/Users/simon/Documents/1-SLP/venv/bin/python',
  NULL,
  '["/Users/simon/Documents/1-SLP/MCP/law-search/server.py"]',
  '{"PYTHONUNBUFFERED":"1","TOKENIZERS_PARALLELISM":"false"}',
  NULL,
  'local',
  NULL, NULL, NULL, 0, 240000,
  NULL, NULL, NULL, NULL, NULL,
  NULL, NULL, 0,
  100, 1, 'manual', 1, 1790310000000,
  1790310000000, 1790310000000, 1790310000000
);

-- 为 法务 助手启用 law-search MCP
INSERT INTO assistant_mcp_server (assistant_id, mcp_server_id, created_at, updated_at)
VALUES ('be1526dd-2b62-4bce-8c91-8e386727bf11', 'law-search-local', 1790310000000, 1790310000000);

-- 为 法律顾问 助手启用 law-search MCP
INSERT INTO assistant_mcp_server (assistant_id, mcp_server_id, created_at, updated_at)
VALUES ('0cbcd5ef-9d8b-4dcc-a81a-6db5856f0f62', 'law-search-local', 1790310000000, 1790310000000);

-- 为 法律文书 助手启用 law-search MCP
INSERT INTO assistant_mcp_server (assistant_id, mcp_server_id, created_at, updated_at)
VALUES ('ef31d659-981b-418e-93c0-e02564ab8290', 'law-search-local', 1790310000000, 1790310000000);
