const url = process.argv[2] || 'ws://127.0.0.1:8876/ws/terminal';
const socket = new WebSocket(url);
let output = '';

const timeout = setTimeout(() => {
  console.error('Timed out waiting for terminal output:', JSON.stringify(output));
  process.exit(2);
}, 12000);

socket.onmessage = event => {
  const message = JSON.parse(event.data);
  if (message.type === 'ready') {
    console.log(`READY ${message.backend}`);
    socket.send(JSON.stringify({type: 'resize', columns: 100, rows: 30}));
    socket.send(JSON.stringify({type: 'input', data: "Write-Output 'E2E中文终端'; exit\r"}));
  }
  if (message.type === 'output') {
    output += message.data;
    if (output.includes('E2E中文终端')) {
      console.log('OUTPUT_OK');
      clearTimeout(timeout);
      socket.close();
      setTimeout(() => process.exit(0), 100);
    }
  }
};

socket.onerror = () => {
  console.error('WebSocket connection failed');
  process.exit(3);
};
