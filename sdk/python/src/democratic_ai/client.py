"""
Client for interacting with Democratic AI platform
"""

import asyncio
import json
from typing import Dict, Any, Optional, List
import httpx
import websockets

from .exceptions import DemocraticAIError, AuthenticationError, TimeoutError


class DemocraticAIClient:
    """
    Client for interacting with Democratic AI platform
    
    Example:
        client = DemocraticAIClient(
            api_key="your-api-key",
            base_url="https://api.democratic-ai.com"
        )
        
        result = await client.execute(
            action="text.summarize",
            payload={"text": "Long text here..."}
        )
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        jwt_token: Optional[str] = None,
        base_url: str = "http://localhost:8000",
        ws_url: str = "ws://localhost:8765",
        timeout: int = 30
    ):
        if not api_key and not jwt_token:
            raise ValueError("Either api_key or jwt_token must be provided")
            
        self.api_key = api_key
        self.jwt_token = jwt_token
        self.base_url = base_url
        self.ws_url = ws_url
        self.timeout = timeout
        
        # HTTP client
        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key
        if jwt_token:
            headers["Authorization"] = f"Bearer {jwt_token}"
            
        self.http_client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=timeout
        )
        
        # WebSocket connection
        self.websocket = None
        
    async def execute(
        self,
        action: str,
        payload: Dict[str, Any],
        module_id: Optional[str] = None,
        stream: bool = False,
        timeout: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Execute a module action
        
        Args:
            action: The action to execute (e.g., "text.summarize")
            payload: The request payload
            module_id: Optional specific module ID
            stream: Whether to stream the response
            timeout: Optional timeout override
            
        Returns:
            The execution result
        """
        request_data = {
            "action": action,
            "payload": payload,
            "stream": stream,
            "timeout": timeout or self.timeout
        }
        
        if module_id:
            request_data["module_id"] = module_id
            
        try:
            response = await self.http_client.post(
                "/api/v1/execute",
                json=request_data
            )
            response.raise_for_status()
            
            result = response.json()
            
            if result.get("status") == "error":
                raise DemocraticAIError(result.get("error", "Unknown error"))
                
            return result.get("result", {})
            
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise AuthenticationError("Authentication failed")
            elif e.response.status_code == 429:
                raise TimeoutError("Rate limit exceeded")
            else:
                raise DemocraticAIError(f"HTTP error: {e}")
        except httpx.TimeoutException:
            raise TimeoutError("Request timed out")
        except Exception as e:
            raise DemocraticAIError(f"Unexpected error: {e}")
            
    async def stream_execute(
        self,
        action: str,
        payload: Dict[str, Any],
        module_id: Optional[str] = None
    ):
        """
        Execute a module action with streaming response
        
        Args:
            action: The action to execute
            payload: The request payload
            module_id: Optional specific module ID
            
        Yields:
            Stream data chunks
        """
        request_data = {
            "action": action,
            "payload": payload,
            "stream": True
        }
        
        if module_id:
            request_data["module_id"] = module_id
            
        async with self.http_client.stream(
            "POST",
            "/api/v1/execute",
            json=request_data
        ) as response:
            response.raise_for_status()
            
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    yield data
                    
    async def list_modules(
        self,
        capability: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        List available modules
        
        Args:
            capability: Optional capability filter
            
        Returns:
            List of modules
        """
        params = {}
        if capability:
            params["capability"] = capability
            
        response = await self.http_client.get(
            "/api/v1/modules",
            params=params
        )
        response.raise_for_status()
        
        return response.json()
        
    async def get_module(self, module_id: str) -> Dict[str, Any]:
        """
        Get module details
        
        Args:
            module_id: The module ID
            
        Returns:
            Module details
        """
        response = await self.http_client.get(f"/api/v1/modules/{module_id}")
        response.raise_for_status()
        
        return response.json()
        
    async def get_usage(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get usage statistics
        
        Args:
            start_date: Optional start date (ISO format)
            end_date: Optional end date (ISO format)
            
        Returns:
            Usage statistics
        """
        params = {}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
            
        response = await self.http_client.get(
            "/api/v1/usage",
            params=params
        )
        response.raise_for_status()
        
        return response.json()
        
    async def get_billing(self) -> Dict[str, Any]:
        """
        Get billing information
        
        Returns:
            Billing information
        """
        response = await self.http_client.get("/api/v1/billing")
        response.raise_for_status()
        
        return response.json()
        
    async def connect_websocket(self):
        """Connect to WebSocket for real-time communication"""
        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
            
        self.websocket = await websockets.connect(
            self.ws_url,
            extra_headers=headers
        )
        
        # Send authentication
        auth_message = {
            "type": "auth",
            "token": self.jwt_token or self.api_key
        }
        await self.websocket.send(json.dumps(auth_message))
        
        # Wait for acknowledgment
        response = await self.websocket.recv()
        response_data = json.loads(response)
        
        if response_data.get("type") != "connected":
            raise ConnectionError("Failed to connect to WebSocket")
            
    async def send_websocket_message(self, message: Dict[str, Any]):
        """Send a message through WebSocket"""
        if not self.websocket:
            await self.connect_websocket()
            
        await self.websocket.send(json.dumps(message))
        
    async def receive_websocket_message(self) -> Dict[str, Any]:
        """Receive a message from WebSocket"""
        if not self.websocket:
            await self.connect_websocket()
            
        message = await self.websocket.recv()
        return json.loads(message)
        
    async def close(self):
        """Close connections"""
        await self.http_client.aclose()
        
        if self.websocket:
            await self.websocket.close()
            
    async def __aenter__(self):
        """Async context manager entry"""
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()


# Example usage
async def main():
    # Create client
    async with DemocraticAIClient(api_key="your-api-key") as client:
        # Execute a module
        result = await client.execute(
            action="text.summarize",
            payload={
                "text": "This is a long text that needs to be summarized...",
                "max_length": 100
            }
        )
        print(f"Summary: {result}")
        
        # List modules
        modules = await client.list_modules(capability="text.summarize")
        print(f"Available modules: {modules}")
        
        # Get usage
        usage = await client.get_usage()
        print(f"Usage: {usage}")


if __name__ == "__main__":
    asyncio.run(main())
