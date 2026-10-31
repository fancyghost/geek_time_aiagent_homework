/**
 * 项目级 pi 扩展入口（自动发现）。
 *
 * pi 在「受信任的项目」里会自动加载 .pi/extensions/*.ts。此文件是一个转发 shim：
 * 它把本作业 pi_memory 长期记忆扩展（../pi_memory_ext/extension.ts）的默认工厂原样导出，
 * 于是从 homework_week4 目录启动 pi 时，memory_write/find/update/delete 四个工具即被注册，
 * 且默认记忆库根为 <cwd>/memory_store —— 与 Python pi_memory 共享同一份磁盘记忆。
 *
 * 也可用 `pi -e ./pi_memory_ext/extension.ts` 快速加载，或依赖 pi_memory_ext/package.json
 * 里的 "pi": {"extensions": ["./extension.ts"]} 清单发现。
 */
export { default } from "../../pi_memory_ext/extension.ts";
