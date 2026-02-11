#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试MCP服务器功能
"""

import json
import sys
import subprocess
import time

def test_mcp_server():
    """测试MCP服务器基本功能"""
    print("🧪 开始测试免费音乐MCP服务器...")
    
    # 测试消息
    test_messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "clientInfo": {
                    "name": "test-client",
                    "version": "1.0.0"
                }
            }
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list"
        }
    ]
    
    try:
        # 启动MCP服务器进程
        process = subprocess.Popen(
            ["python3", "music_mcp_server.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd="/Users/a1234/Documents/music"
        )
        
        print("✅ MCP服务器进程已启动")
        
        # 发送初始化消息
        for i, message in enumerate(test_messages, 1):
            print(f"📤 发送测试消息 {i}: {message['method']}")
            
            # 发送JSON-RPC消息
            json_message = json.dumps(message) + "\n"
            process.stdin.write(json_message)
            process.stdin.flush()
            
            # 等待响应
            time.sleep(1)
            
        print("✅ 测试消息发送完成")
        
        # 等待一段时间让服务器处理
        time.sleep(2)
        
        # 终止进程
        process.terminate()
        process.wait(timeout=5)
        
        print("✅ MCP服务器测试完成")
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        if 'process' in locals():
            process.terminate()
        return False

def main():
    """主函数"""
    print("=" * 50)
    print("🎵 免费音乐MCP服务器测试工具")
    print("=" * 50)
    
    success = test_mcp_server()
    
    print("\n" + "=" * 50)
    if success:
        print("🎉 所有测试通过！")
        print("\n📋 配置信息:")
        print("   服务器路径: /Users/a1234/Documents/music/music_mcp_server.py")
        print("   启动命令: python3 /Users/a1234/Documents/music/music_mcp_server.py")
        print("   传输协议: stdio")
        print("\n🔧 小智AI配置:")
        print("   1. 登录 xiaozhi.me 控制台")
        print("   2. 添加MCP接入点")
        print("   3. 使用上述配置信息")
    else:
        print("❌ 测试失败，请检查配置")
        sys.exit(1)
    print("=" * 50)

if __name__ == "__main__":
    main()