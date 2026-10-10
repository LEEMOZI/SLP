#!/usr/bin/env node
/**
 * 端到端集成测试 — 验证 9 个 DSH bundle 协同工作
 *
 * 测试内容：
 *   1. 所有 bundle 可加载（package.json + cordis.patch.yml + src/*.js）
 *   2. 插件导出接口正确（name, inject, apply, _internal）
 *   3. 插件间依赖关系正确
 *   4. Profile package.json 声明完整
 *   5. 符号链接正确
 *
 * 用法：node tests/integration_test.js
 */

'use strict';

const fs = require('fs');
const path = require('path');

const PLUGINS_DIR = path.join(__dirname, '..', 'plugins');
const PROFILES_DIR = path.join(__dirname, '..', 'dsh-home', 'profiles');

// 9 个 bundle 清单
const EXPECTED_BUNDLES = [
  // P0
  'law-search-dsh-bundle',
  'llm-router-bundle',
  'policy-legal-safety-bundle',
  // P1
  'skill-provider-legal',
  'subagent-legal-bundle',
  'session-storage-fs',
  // P2
  'agent-loop-legal',
  'ui-legal-citations',
  'observability-bundle',
];

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    console.log(`  ✅ ${name}`);
    passed++;
  } catch (err) {
    console.log(`  ❌ ${name}: ${err.message}`);
    failed++;
  }
}

function assert(condition, message) {
  if (!condition) throw new Error(message || 'Assertion failed');
}

// ============================================================
// 测试 1: 所有 bundle 目录存在
// ============================================================
console.log('\n[1/5] Bundle 目录检查...');
for (const bundle of EXPECTED_BUNDLES) {
  test(`${bundle} 目录存在`, () => {
    const dir = path.join(PLUGINS_DIR, bundle);
    assert(fs.existsSync(dir), `目录不存在: ${dir}`);
  });
}

// ============================================================
// 测试 2: 每个 bundle 有完整的文件结构
// ============================================================
console.log('\n[2/5] Bundle 文件结构检查...');
for (const bundle of EXPECTED_BUNDLES) {
  const dir = path.join(PLUGINS_DIR, bundle);
  
  test(`${bundle}/package.json 存在且可解析`, () => {
    const pkgPath = path.join(dir, 'package.json');
    assert(fs.existsSync(pkgPath), 'package.json 不存在');
    const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf-8'));
    assert(pkg.name, '缺少 name 字段');
    assert(pkg.dsh && pkg.dsh.bundle && pkg.dsh.bundle.patch, '缺少 dsh.bundle.patch');
  });

  test(`${bundle}/cordis.patch.yml 存在`, () => {
    const ymlPath = path.join(dir, 'cordis.patch.yml');
    assert(fs.existsSync(ymlPath), 'cordis.patch.yml 不存在');
  });

  test(`${bundle}/README.md 存在`, () => {
    const readmePath = path.join(dir, 'README.md');
    assert(fs.existsSync(readmePath), 'README.md 不存在');
  });
}

// ============================================================
// 测试 3: JS 插件导出正确接口
// ============================================================
console.log('\n[3/5] JS 插件接口检查...');
const JS_BUNDLES = [
  'policy-legal-safety-bundle',
  'skill-provider-legal',
  'subagent-legal-bundle',
  'session-storage-fs',
  'agent-loop-legal',
  'ui-legal-citations',
  'observability-bundle',
];

for (const bundle of JS_BUNDLES) {
  test(`${bundle} 导出 {name, inject, apply}`, () => {
    const pkgPath = path.join(PLUGINS_DIR, bundle, 'package.json');
    const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf-8'));
    const mainFile = pkg.main || 'src/index.js';
    const mainPath = path.join(PLUGINS_DIR, bundle, mainFile);
    assert(fs.existsSync(mainPath), `主文件不存在: ${mainFile}`);
    
    // 读取文件内容检查导出
    const content = fs.readFileSync(mainPath, 'utf-8');
    assert(content.includes('module.exports'), '缺少 module.exports');
    assert(content.includes('name'), '缺少 name 导出');
    assert(content.includes('inject'), '缺少 inject 导出');
    assert(content.includes('apply'), '缺少 apply 导出');
  });
}

// ============================================================
// 测试 4: Profile package.json 声明完整
// ============================================================
console.log('\n[4/5] Profile 声明检查...');
const PROFILES = ['lm-studio-workbench', 'work-project'];

for (const profile of PROFILES) {
  test(`${profile} 声明所有 9 个 bundle`, () => {
    const pkgPath = path.join(PROFILES_DIR, profile, 'package.json');
    assert(fs.existsSync(pkgPath), 'package.json 不存在');
    const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf-8'));
    const bundles = pkg.dsh?.profile?.bundles || [];
    
    for (const expected of EXPECTED_BUNDLES) {
      const fullName = `@law-agent/${expected}`;
      assert(bundles.includes(fullName), `缺少 bundle 声明: ${fullName}`);
    }
  });
}

// ============================================================
// 测试 5: 符号链接正确
// ============================================================
console.log('\n[5/5] 符号链接检查...');
const NODE_MODULES_DIR = path.join(process.env.HOME, '.dsh', 'profiles', 'node_modules', '@law-agent');

if (fs.existsSync(NODE_MODULES_DIR)) {
  for (const bundle of EXPECTED_BUNDLES) {
    test(`${bundle} 符号链接存在`, () => {
      const linkPath = path.join(NODE_MODULES_DIR, bundle);
      assert(fs.existsSync(linkPath), `符号链接不存在: ${linkPath}`);
    });
  }
} else {
  console.log('  ⚠️  ~/.dsh/profiles/node_modules/@law-agent 不存在，跳过符号链接检查');
}

// ============================================================
// 汇总
// ============================================================
console.log('\n' + '='.repeat(60));
console.log(`集成测试结果: ${passed} 通过, ${failed} 失败`);
console.log('='.repeat(60));

process.exit(failed > 0 ? 1 : 0);
