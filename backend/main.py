import asyncio
import websockets
import json

connected_players = set()

async def handler(websocket):
    connected_players.add(websocket)
    # Store temporary placeholder attribute directly on session socket context tracking
    websocket.player_id = None 
    print(f"Player joined! Total: {len(connected_players)}")
    
    try:
        async for message in websocket:
            data = json.loads(message)
            
            # Intercept identity handshakes and map tracking attributes to connection reference
            if data.get("type") in ["join", "reply"] and "id" in data:
                websocket.player_id = data["id"]

            # Broadcast actions to other players
            for player in connected_players:
                if player != websocket:
                    await player.send(json.dumps(data))
                    
    except websockets.ConnectionClosed:
        pass
    finally:
        connected_players.remove(websocket)
        print(f"Player disconnected! Total: {len(connected_players)}")
        
        # If the closed player had a valid ID, notify remaining users to drop them
        if websocket.player_id:
            leave_notification = {
                "type": "leave",
                "id": websocket.player_id
            }
            for player in connected_players:
                try:
                    await player.send(json.dumps(leave_notification))
                except Exception:
                    pass # Fail-safe wrapper ignoring messaging transmission faults on separate dropping pipelines

async def main():
    print("Server running on port 10000...")
    async with websockets.serve(handler, "0.0.0.0", 10000):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())