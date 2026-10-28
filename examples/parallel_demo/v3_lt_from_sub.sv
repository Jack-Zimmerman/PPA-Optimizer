module alu_p (input logic clk, rst, input logic [7:0] a, b, input logic [2:0] op, output logic [7:0] y, output logic lt);
  logic [7:0] n;
  logic [8:0] diff;
  assign diff = {1'b0, a} - {1'b0, b};
  always_comb case (op)
    0: n = a + b;  1: n = diff[7:0];  2: n = a & b;  3: n = a | b;
    4: n = a ^ b;  5: n = a << b[2:0];  6: n = a >> b[2:0];  default: n = 0;
  endcase
  always_ff @(posedge clk) if (rst) begin y <= 0; lt <= 0; end else begin y <= n; lt <= diff[8]; end
endmodule
