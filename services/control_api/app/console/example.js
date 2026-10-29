"use strict";
// Browser-only examples. These records are never sent to the Control API.
const exampleModel = {id:1, model_name:"智能电表（示例）", vendor_name:"Demo Instruments", model_code:"DEMO-METER-100", status:"active"};
const exampleProfile = {id:1, device_model_id:1, profile_name:"电力监测标准配置", version:"1.0.0", status:"released", is_default:true};
const exampleDevice = {id:1, device_model_id:1, profile_version_id:1, instance_name:"配电柜电表（示例）", instance_code:"DEMO-001", location:"演示工作区", status:"active"};
const examplePoints = [
  {id:1, name:"电压", code:"voltage", address_text:"30001", data_type:"float32", access_mode:"R", unit:"V"},
  {id:2, name:"电流", code:"current", address_text:"30007", data_type:"float32", access_mode:"R", unit:"A"},
  {id:3, name:"有功功率", code:"active_power", address_text:"30013", data_type:"float32", access_mode:"R", unit:"W"},
];
const exampleCapability = {id:1, name:"电力监测", code:"power_monitor", capability_kind:"observe", description:"读取电压、电流和有功功率"};
const exampleCommands = [{id:1, command_name:"读取电力参数", command_code:"power_monitor.read", access_mode:"R", operations:examplePoints.map(p=>({op:"read", point_id:p.id, point_code:p.code}))}];
const exampleRoutes = {
  "/api/v1/device-models":[exampleModel],
  "/api/v1/profiles":[exampleProfile, {id:2, device_model_id:1, profile_name:"扩展配置（示例草稿）", version:"1.1.0", status:"draft", is_default:false}],
  "/api/v1/capabilities":[exampleCapability],
  "/api/v1/devices":[exampleDevice],
  "/api/v1/profiles/1":exampleProfile,
  "/api/v1/profiles/2":{id:2, profile_name:"扩展配置（示例草稿）", version:"1.1.0", status:"draft"},
  "/api/v1/profiles/1/points":examplePoints,
  "/api/v1/profiles/2/points":[],
  "/api/v1/profiles/1/capabilities":[{capability_name:"电力监测",capability_code:"power_monitor",bindings:examplePoints.map(p=>({role:"measurement",point_code:p.code}))}],
  "/api/v1/profiles/2/capabilities":[],
  "/api/v1/profiles/1/commands":exampleCommands,
  "/api/v1/profiles/2/commands":[],
  "/api/v1/devices/1/context":{device:exampleDevice, profile:exampleProfile, connections:[{id:1, name:"Modbus TCP 示例连接", protocol_spec_id:1, status:"pending"}]},
  "/api/v1/devices/1/test-options":{capabilities:[{name:"电力监测",code:"power_monitor",commands:["power_monitor.read"]}],points:examplePoints},
  "/api/v1/connections/1/validate-config":{status:"valid",errors:[]},
};
