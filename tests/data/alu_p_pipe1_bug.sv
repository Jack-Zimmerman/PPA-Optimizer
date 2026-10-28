// alu_p with its inputs registered: same function, one extra cycle of output latency.
module alu_p (input logic clk, rst, input logic [7:0] a, b, input logic [2:0] op, output logic [7:0] y, output logic lt);
  logic [7:0] aq, bq, n;
  logic [2:0] opq;
  always_ff @(posedge clk) begin aq <= a; bq <= b; opq <= op; end   // pipeline stage, deliberately not reset
  always_comb case (opq)
    0: n = aq + bq;  1: n = aq - bq;  2: n = aq & bq;  3: n = aq | bq;
    4: n = aq ^ bq;  5: n = aq << bq[2:0];  6: n = aq >> bq[2:0];  default: n = 0;
  endcase
  always_ff @(posedge clk) if (rst) begin y <= 0; lt <= 0; end else begin y <= n; lt <= aq <= bq; end
endmodule
