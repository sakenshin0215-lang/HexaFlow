import asyncio
import os
from hexaflow.core.engine import HexaEngine

async def main():
    
    trace_file = "memory/workspace/traces/ManualTask_20260303_192606_20260303_192745.json" 
    
    engine = HexaEngine(headless=False) 
    await engine.start()
    
    try:
        report_paths = await engine.run_from_trace(trace_path=trace_file)
        if report_paths:
            print(f"\n📄 报告(JSON): {report_paths['json_path']}")
            print(f"📝 报告(MD):   {report_paths['md_path']}")
        
        input("\n执行完毕，按回车键退出...")
    finally:
        await engine.stop()

if __name__ == "__main__":
    asyncio.run(main())
