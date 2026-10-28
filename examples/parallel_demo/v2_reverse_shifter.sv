module alu_p (input logic clk, rst, input logic [7:0] a, b, input logic [2:0] op, output logic [7:0] y, output logic lt);
  logic [7:0] n, addsub, rev_in, shifted, rev_out;
  assign addsub = a + (b ^ {8{op[0]}}) + op[0];
  always_comb for (int i = 0; i < 8; i++) begin
    rev_in[i]  = (op == 3'd5) ? a[7-i] : a[i];      // reverse for left shift
    rev_out[i] = (op == 3'd5) ? shifted[7-i] : shifted[i];
  end
  assign shifted = rev_in >> b[2:0];                 // one right shifter serves both directions
  always_comb case (op)
    0, 1: n = addsub;  2: n = a & b;  3: n = a | b;
    4: n = a ^ b;  5, 6: n = rev_out;  default: n = 0;
  endcase
  always_ff @(posedge clk) if (rst) begin y <= 0; lt <= 0; end else begin y <= n; lt <= a < b; end
endmodule
