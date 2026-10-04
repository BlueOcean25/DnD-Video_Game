import asyncio
import websockets
import json

connected_players = set()

async def handler(websocket):
    connected_players.add(websocket)
    print(f"Player joined! Total: {len(connected_players)}")
    try:
        async for message in websocket:
            data = json.loads(message)
            # Broadcast actions to other players
            for player in connected_players:
                if player != websocket:
                    await player.send(json.dumps(data))
    except websockets.ConnectionClosed:
        pass
    finally:
        connected_players.remove(websocket)

async def main():
    print("Server running on port 10000...")
    async with websockets.serve(handler, "0.0.0.0", 10000):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
