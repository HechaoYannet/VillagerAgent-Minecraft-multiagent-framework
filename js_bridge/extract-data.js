/**
 * minecraft-data 提取脚本
 *
 * 从 minecraft-data 1.21.1 提取方块、物品、配方、实体数据，
 * 并生成与 VillagerAgent Python 代码兼容的 JSON 文件。
 *
 * 用法: node extract-data.js [输出目录]
 * 默认输出: ../data/
 */

const fs = require('fs');
const path = require('path');
const mcData = require('minecraft-data')('1.21.1');

const OUT_DIR = process.argv[2] || path.join(__dirname, '..', 'data');

// 确保输出目录存在
if (!fs.existsSync(OUT_DIR)) {
  fs.mkdirSync(OUT_DIR, { recursive: true });
}

console.log(`Minecraft 版本: ${mcData.version.minecraftVersion}`);
console.log(`输出版本: ${mcData.version.version}`);
console.log(`输出目录: ${OUT_DIR}`);

// ============================================================
// 1. 提取方块数据 (blocks.json)
// ============================================================
function extractBlocks() {
  console.log(`\n提取方块数据...`);
  const blocks = mcData.blocksArray.map(b => ({
    id: b.id,
    name: b.name,
    displayName: b.displayName,
    hardness: b.hardness || 0,
    resistance: b.resistance || 0,
    stackSize: b.stackSize || 64,
    diggable: b.diggable !== undefined ? b.diggable : true,
    material: b.material || null,
    transparent: b.transparent || false,
    emitLight: b.emitLight || 0,
    filterLight: b.filterLight || 15,
    defaultState: b.defaultState || b.id,
    minStateId: b.minStateId || b.id,
    maxStateId: b.maxStateId || b.id,
    states: b.states || [],
    harvestTools: b.harvestTools || {},
    drops: b.drops || [],
    boundingBox: b.boundingBox || 'block'
  }));

  const file = path.join(OUT_DIR, 'blocks.json');
  fs.writeFileSync(file, JSON.stringify(blocks, null, 2), 'utf-8');
  console.log(`  ✓ blocks.json: ${blocks.length} 个方块`);
  return blocks;
}

// ============================================================
// 2. 提取物品数据 (items.json)
// ============================================================
function extractItems() {
  console.log(`\n提取物品数据...`);
  const items = mcData.itemsArray.map(i => ({
    id: i.id,
    name: i.name,
    displayName: i.displayName,
    stackSize: i.stackSize || 64
  }));

  const file = path.join(OUT_DIR, 'items.json');
  fs.writeFileSync(file, JSON.stringify(items, null, 2), 'utf-8');
  console.log(`  ✓ items.json: ${items.length} 个物品`);
  return items;
}

// ============================================================
// 3. 提取配方数据 (recipes.json)
// ============================================================
function extractRecipes() {
  console.log(`\n提取配方数据...`);

  // 创建 ID → name 的映射
  const idToName = {};
  for (const item of mcData.itemsArray) {
    idToName[item.id] = item.name;
  }

  const recipes = [];
  const recipeEntries = Object.entries(mcData.recipes);

  for (const [resultIdStr, variants] of recipeEntries) {
    for (const variant of variants) {
      const recipe = {
        result: {
          name: idToName[variant.result.id] || `id_${variant.result.id}`,
          count: variant.result.count || 1
        },
        ingredients: []
      };

      // 收集所有唯一原料
      const ingredientIds = new Set();
      if (variant.ingredients) {
        for (const id of variant.ingredients) {
          if (id !== null && id !== undefined && id !== -1) {
            ingredientIds.add(id);
          }
        }
      }
      if (variant.inShape) {
        for (const row of variant.inShape) {
          for (const id of row) {
            if (id !== null && id !== undefined && id !== -1) {
              ingredientIds.add(id);
            }
          }
        }
      }

      // 计算每种原料的总数量
      const ingredientCounts = {};
      const countInArray = (arr) => {
        for (const item of arr) {
          if (item !== null && item !== undefined && item !== -1) {
            const name = idToName[item] || `id_${item}`;
            ingredientCounts[name] = (ingredientCounts[name] || 0) + 1;
          }
        }
      };

      if (variant.ingredients) countInArray(variant.ingredients);
      if (variant.inShape) {
        for (const row of variant.inShape) countInArray(row);
      }

      recipe.ingredients = Object.entries(ingredientCounts).map(([name, count]) => ({
        name,
        count
      }));

      // 处理 inShape (将 ID 转换为 name)
      if (variant.inShape) {
        recipe.inShape = variant.inShape.map(row =>
          row.map(id => {
            if (id === null || id === undefined || id === -1) return null;
            return idToName[id] || `id_${id}`;
          })
        );
      }

      recipes.push(recipe);
    }
  }

  const file = path.join(OUT_DIR, 'recipes.json');
  fs.writeFileSync(file, JSON.stringify(recipes, null, 2), 'utf-8');
  console.log(`  ✓ recipes.json: ${recipes.length} 个配方`);
  return recipes;
}

// ============================================================
// 4. 提取动物/实体数据 (animals.json)
// ============================================================
function extractAnimals() {
  console.log(`\n提取动物数据...`);
  const animals = mcData.entitiesArray
    .filter(e => e.type === 'animal' || (e.category && (
      e.category.includes('Passive') ||
      e.category.includes('Animal') ||
      e.category.includes('Neutral')
    )))
    .map(e => ({
      id: e.id,
      internalId: e.internalId || e.id,
      name: e.name,
      displayName: e.displayName,
      width: e.width || 0.6,
      height: e.height || 0.6,
      type: e.type || 'animal',
      category: e.category || 'Passive mobs'
    }));

  const file = path.join(OUT_DIR, 'animals.json');
  fs.writeFileSync(file, JSON.stringify(animals, null, 2), 'utf-8');
  console.log(`  ✓ animals.json: ${animals.length} 个实体`);
  return animals;
}

// ============================================================
// 5. 提取挖掘工具数据 (dig_item.json)
// ============================================================
function extractDigItems(blocks) {
  console.log(`\n提取挖掘工具数据...`);

  // 工具名称映射 (minecraft-data 中的 harvestTools 键是物品 ID)
  const itemById = {};
  for (const item of mcData.itemsArray) {
    itemById[item.id] = item.name;
  }

  // 已知的工具名称列表
  const knownTools = [
    'wooden_pickaxe', 'stone_pickaxe', 'iron_pickaxe', 'golden_pickaxe',
    'diamond_pickaxe', 'netherite_pickaxe',
    'wooden_axe', 'stone_axe', 'iron_axe', 'golden_axe',
    'diamond_axe', 'netherite_axe',
    'wooden_shovel', 'stone_shovel', 'iron_shovel', 'golden_shovel',
    'diamond_shovel', 'netherite_shovel',
    'wooden_hoe', 'stone_hoe', 'iron_hoe', 'golden_hoe',
    'diamond_hoe', 'netherite_hoe',
    'wooden_sword', 'stone_sword', 'iron_sword', 'golden_sword',
    'diamond_sword', 'netherite_sword',
    'shears'
  ];

  const digItems = blocks.map(b => {
    const entry = {
      name: b.name,
      diggable: b.diggable
    };

    // 转换 harvestTools 的 ID 键为名称
    if (b.harvestTools && Object.keys(b.harvestTools).length > 0) {
      const tools = [];
      for (const toolIdStr of Object.keys(b.harvestTools)) {
        const toolId = parseInt(toolIdStr);
        const toolName = itemById[toolId];
        if (toolName && knownTools.includes(toolName)) {
          tools.push(toolName);
        }
      }
      if (tools.length > 0) {
        entry.tools = tools;
      }
    }

    return entry;
  });

  const file = path.join(OUT_DIR, 'dig_item.json');
  // 保存为紧凑 JSON(单行),匹配原始格式
  fs.writeFileSync(file, JSON.stringify(digItems), 'utf-8');
  console.log(`  ✓ dig_item.json: ${digItems.length} 个方块`);
  return digItems;
}

// ============================================================
// 6. 更新 mcData.json (双语实体名称映射)
// ============================================================
function updateMcData() {
  console.log(`\n更新实体名称映射...`);

  // 读取现有的 mcData.json 以保留中文翻译
  const mcDataFile = path.join(OUT_DIR, 'mcData.json');
  let existingEntities = {};
  try {
    const existing = JSON.parse(fs.readFileSync(mcDataFile, 'utf-8'));
    if (existing.entities) {
      for (const [en, zh] of existing.entities) {
        existingEntities[en] = zh;
      }
    }
  } catch (e) {
    console.log('  未找到现有 mcData.json,将创建新的');
  }

  // 为 1.21.1 中的新实体补充中文翻译
  const newTranslations = {
    // 1.21 新增
    'armadillo': '犰狳',
    'bogged': '沼泽骷髅',
    'breeze': '旋风人',
    'breeze_wind_charge': '旋风弹',
    'wind_charge': '风弹',
    'trial_spawner': '试炼刷怪笼',
    'vault': '宝库',
    'ominous_item_spawner': '不祥物品刷怪笼',
    'ominous_vault': '不祥宝库',
    // 1.20 新增(如果旧文件没有)
    'camel': '骆驼',
    'sniffer': '嗅探兽',
    'cherry_boat': '樱花木船',
    'cherry_chest_boat': '樱花木运输船',
    'bamboo_raft': '竹筏',
    'bamboo_chest_raft': '竹箱筏',
    'decorated_pot': '饰纹陶罐',
  };

  Object.assign(existingEntities, newTranslations);

  // 生成实体列表
  const entities = mcData.entitiesArray.map(e => {
    const zh = existingEntities[e.name];
    return zh ? [e.name, zh] : [e.name, e.displayName];
  });

  // 保留 manually curated 的实体翻译
  const mcDataJson = {
    entities
  };

  fs.writeFileSync(mcDataFile, JSON.stringify(mcDataJson, null, 2), 'utf-8');
  console.log(`  ✓ mcData.json: ${entities.length} 个实体 (含 ${Object.keys(newTranslations).filter(k => !existingEntities[k]).length} 个新翻译)`);
  return mcDataJson;
}

// ============================================================
// 执行
// ============================================================
console.log('='.repeat(50));
console.log('VillagerAgent minecraft-data 提取工具');
console.log('='.repeat(50));

const blocks = extractBlocks();
const items = extractItems();
const recipes = extractRecipes();
const animals = extractAnimals();
const digItems = extractDigItems(blocks);
const mcDataJson = updateMcData();

// 保留现有的 goal_lib.json 和 name_list.json (手动管理)
const preserveFiles = ['goal_lib.json', 'name_list.json'];
for (const f of preserveFiles) {
  const src = path.join(OUT_DIR, f);
  if (fs.existsSync(src)) {
    console.log(`  ✓ ${f}: 已保留 (手动管理)`);
  }
}

console.log(`\n${'='.repeat(50)}`);
console.log('提取完成!');
console.log(`  方块: ${blocks.length}`);
console.log(`  物品: ${items.length}`);
console.log(`  配方: ${recipes.length}`);
console.log(`  动物: ${animals.length}`);
console.log(`  挖掘数据: ${digItems.length}`);
console.log(`  实体映射: ${mcDataJson.entities.length}`);
console.log(`  输出目录: ${OUT_DIR}`);
