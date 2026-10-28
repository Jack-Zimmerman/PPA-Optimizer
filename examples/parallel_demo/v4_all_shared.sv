module alu_p (input logic clk, rst, input logic [7:0] a, b, input logic [2:0] op, output logic [7:0] y, output logic lt);
  logic [7:0] n;
  logic [8:0] addsub;
  assign addsub = {1'b0, a} + {1'b0, b ^ {8{op[0]}}} + op[0];
  always_comb case (op)
    0, 1: n = addsub[7:0];  2: n = a & b;  3: n = a | b;
    4: n = a ^ b;  5: n = a << b[2:0];  6: n = a >> b[2:0];  default: n = 0;
  endcase
  always_ff @(posedge clk) if (rst) begin y <= 0; lt <= 0; end else begin y <= n; lt <= ~addsub[8]; end   // WRONG on purpose: lt only valid for sub
endmodule
