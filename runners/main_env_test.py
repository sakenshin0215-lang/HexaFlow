import asyncio
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexaflow.core.engine import HexaEngine

async def main():
    # ⚠️ 替换为你刚才录制好的、带有 "is_optional" 逻辑的最新 JSON 文件路径
    trace_file = "memory/workspace/traces/Task_20260303_160332_20260303_160502.json" 
    
    engine = HexaEngine(headless=False)
    await engine.start()
    
    # ==========================================
    # 定义测试环境矩阵 (Matrix Testing)
    # ==========================================
    environments = [
        {
            "name": "桌面端 - 老用户 (带缓存)",
            "viewport": {'width': 1280, 'height': 800},
            "state_path": "memory/workspace/auth_state.json", # 会跳过风险提示
            "user_agent": None
        },
        {
            "name": "桌面端 - 新用户 (纯净环境)",
            "viewport": {'width': 1280, 'height': 800},
            "state_path": "memory/workspace/fake_empty_state.json", # 故意给个空路径，强制弹出风险提示
            "user_agent": None
        },
        {
            "name": "移动端 (iPhone 13) - 新用户",
            "viewport": {'width': 390, 'height': 844}, # 手机屏幕尺寸
            "state_path": "memory/workspace/fake_empty_state_mobile.json",
            "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
        }
    ]

    try:
        for env in environments:
            print(f"\n" + "="*50)
            print(f"🚀 正在测试环境: {env['name']}")
            print(f"   分辨率: {env['viewport']}")
            print(f"   缓存文件: {env['state_path']}")
            print("="*50)
            
            try:
                # 把环境参数动态传给引擎
                await engine.run_from_trace(
                    trace_path=trace_file,
                    viewport=env['viewport'],
                    state_path=env['state_path'],
                    user_agent=env['user_agent']
                )
                print(f"✅ 环境 [{env['name']}] 回放测试通过！\n")
            except Exception as e:
                print(f"❌ 环境 [{env['name']}] 测试失败: {e}\n")
                
            # 跑完一个环境后稍微等 2 秒，方便你观察
            await asyncio.sleep(2)
            
    finally:
        await engine.stop()

if __name__ == "__main__":
    asyncio.run(main())
